"""检查任务调度引擎。

把合规评分固化为有执法人员容量约束的检查任务，并在延期、机构停业、
风险突升、检查结果回写等触发器下事务性地重排任务。

设计要点：
- 任务生成时固定 compliance_score_id（评分依据）与 rule_version_id（频率规则版本），
  之后规则调整不会改写已生成任务的依据；
- 执法人员按日容量约束排期，高优先级（高风险机构）的任务先占用档期，
  避免高风险机构被排到后面；
- 重排在单个数据库事务内完成：先在事务内去重（同一机构同一到期季度的重复
  常规任务）并解除范围内任务排期，再按优先级重排，提交前做一致性校验
  （容量、重复任务），校验失败整体回滚；
- 进行中/已完成/已取消的任务冻结，任何重排都不会改写已执行的任务；
- 无法排期的任务保留为"待排期"缺口并记录明确原因，绝不静默丢弃。
"""

import json
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session

from .models import (
    Institution, ComplianceScore, Inspector, RuleVersion,
    InspectionTask, TaskEvent, TaskStatus, TaskKind,
    TaskEventAction, ReplanTrigger, CluePriority, ComplianceGrade,
)

ACTIVE_TASK_STATUSES = (TaskStatus.PENDING_ASSIGNMENT, TaskStatus.SCHEDULED)
FROZEN_TASK_STATUSES = (
    TaskStatus.IN_PROGRESS, TaskStatus.COMPLETED, TaskStatus.CANCELLED
)

PRIORITY_ORDER = {
    CluePriority.HIGH: 0,
    CluePriority.MEDIUM: 1,
    CluePriority.LOW: 2,
}

GRADE_RANK = {
    ComplianceGrade.EXCELLENT: 0,
    ComplianceGrade.GOOD: 1,
    ComplianceGrade.FAIR: 2,
    ComplianceGrade.POOR: 3,
}

DEFAULT_RULES = {
    "A": {"frequency_months": 24, "priority": "低"},
    "B": {"frequency_months": 12, "priority": "低"},
    "C": {"frequency_months": 6, "priority": "中"},
    "D": {"frequency_months": 3, "priority": "高"},
}

OPERATING_NORMAL = "正常营业"
OPERATING_CLOSED = "停业"


def quarter_key(d: date) -> Tuple[int, int]:
    return (d.year, (d.month - 1) // 3 + 1)


def quarter_label(d: date) -> str:
    y, q = quarter_key(d)
    return f"{y}年Q{q}"


def parse_rules(rule_version: RuleVersion) -> Dict[str, dict]:
    try:
        return json.loads(rule_version.rules)
    except (json.JSONDecodeError, TypeError):
        return {}


def rule_for_grade(rules: Dict[str, dict], grade: ComplianceGrade) -> dict:
    key = grade.value if isinstance(grade, ComplianceGrade) else str(grade)
    rule = rules.get(key) or {}
    return {
        "frequency_months": int(rule.get("frequency_months", 12)),
        "priority": rule.get("priority", CluePriority.LOW.value),
    }


def get_active_rule_version(db: Session) -> Optional[RuleVersion]:
    return db.query(RuleVersion).filter(RuleVersion.is_active == True).first()


def _institution_names(db: Session) -> Dict[int, str]:
    return {i.id: i.name for i in db.query(Institution).all()}


def _decision(
    names: Dict[int, str],
    institution_id: int,
    action: TaskEventAction,
    reason: str,
    task: Optional[InspectionTask] = None,
    old_scheduled_date: Optional[date] = None,
    new_scheduled_date: Optional[date] = None,
    old_inspector_id: Optional[int] = None,
    new_inspector_id: Optional[int] = None,
    old_priority: Optional[CluePriority] = None,
    new_priority: Optional[CluePriority] = None,
) -> dict:
    return {
        "task_id": task.id if task else None,
        "task_no": task.task_no if task else None,
        "institution_id": institution_id,
        "institution_name": names.get(institution_id, f"机构#{institution_id}"),
        "action": action,
        "reason": reason,
        "old_scheduled_date": old_scheduled_date,
        "new_scheduled_date": new_scheduled_date,
        "old_inspector_id": old_inspector_id,
        "new_inspector_id": new_inspector_id,
        "old_priority": old_priority,
        "new_priority": new_priority,
    }


def _record_event(
    db: Session,
    task: InspectionTask,
    trigger: ReplanTrigger,
    action: TaskEventAction,
    reason: str,
    old_scheduled_date: Optional[date] = None,
    new_scheduled_date: Optional[date] = None,
    old_inspector_id: Optional[int] = None,
    new_inspector_id: Optional[int] = None,
    old_priority: Optional[CluePriority] = None,
    new_priority: Optional[CluePriority] = None,
    old_score_id: Optional[int] = None,
    new_score_id: Optional[int] = None,
) -> TaskEvent:
    event = TaskEvent(
        task_id=task.id,
        trigger=trigger,
        action=action,
        reason=reason,
        old_scheduled_date=old_scheduled_date,
        new_scheduled_date=new_scheduled_date,
        old_inspector_id=old_inspector_id,
        new_inspector_id=new_inspector_id,
        old_priority=old_priority,
        new_priority=new_priority,
        old_score_id=old_score_id,
        new_score_id=new_score_id,
    )
    db.add(event)
    return event


def _compose(prefix: Optional[str], body: str) -> str:
    return f"{prefix}；{body}" if prefix else body


def _active_inspectors(db: Session) -> List[Inspector]:
    return db.query(Inspector).filter(Inspector.is_active == True).order_by(Inspector.id).all()


def _build_occupancy(db: Session, exclude_task_ids: Set[int]) -> Dict[Tuple[int, date], int]:
    """统计已占用的 (执法人员, 日期) 容量，排除本次将被重排的任务。"""
    query = db.query(InspectionTask).filter(
        InspectionTask.status.in_([TaskStatus.SCHEDULED, TaskStatus.IN_PROGRESS]),
        InspectionTask.scheduled_date.isnot(None),
        InspectionTask.inspector_id.isnot(None),
    )
    if exclude_task_ids:
        query = query.filter(~InspectionTask.id.in_(exclude_task_ids))
    occupancy: Dict[Tuple[int, date], int] = {}
    for t in query.all():
        key = (t.inspector_id, t.scheduled_date)
        occupancy[key] = occupancy.get(key, 0) + 1
    return occupancy


def _pick_inspector(
    inspectors: List[Inspector],
    occupancy: Dict[Tuple[int, date], int],
    day: date,
) -> Optional[int]:
    """在指定日期挑选剩余容量最多（负载最低）的执法人员，保证确定性。"""
    best: Optional[Tuple[int, int]] = None  # (load, inspector_id)
    for insp in inspectors:
        load = occupancy.get((insp.id, day), 0)
        if load < insp.daily_capacity:
            if best is None or load < best[0] or (load == best[0] and insp.id < best[1]):
                best = (load, insp.id)
    return best[1] if best else None


def _seat_task(
    task: InspectionTask,
    inspectors: List[Inspector],
    occupancy: Dict[Tuple[int, date], int],
    window_start: date,
    window_end: date,
    pinned_date: Optional[date] = None,
    not_before: Optional[date] = None,
) -> bool:
    """为任务分配日期与执法人员。钉住日期的任务必须落在该日期。"""
    if pinned_date is not None:
        inspector_id = _pick_inspector(inspectors, occupancy, pinned_date)
        if inspector_id is None:
            return False
        task.scheduled_date = pinned_date
        task.inspector_id = inspector_id
    else:
        day = window_start
        if not_before and not_before > day:
            day = not_before
        seated = False
        while day <= window_end:
            inspector_id = _pick_inspector(inspectors, occupancy, day)
            if inspector_id is not None:
                task.scheduled_date = day
                task.inspector_id = inspector_id
                seated = True
                break
            day += timedelta(days=1)
        if not seated:
            return False

    task.status = TaskStatus.SCHEDULED
    task.unschedulable_reason = None
    key = (task.inspector_id, task.scheduled_date)
    occupancy[key] = occupancy.get(key, 0) + 1
    return True


def _task_sort_key(task: InspectionTask):
    return (
        PRIORITY_ORDER.get(task.priority, 1),
        task.due_date or date.max,
        task.id,
    )


def create_task_candidates(
    db: Session,
    period_start: date,
    period_end: date,
    rule_version: RuleVersion,
    institution_id: Optional[int] = None,
) -> Tuple[List[int], List[dict]]:
    """按机构最新评分与规则版本生成待排期任务候选。

    返回 (新任务ID列表, 前置决策列表)。同一机构同一到期季度已存在
    未取消的常规任务时不重复生成。
    """
    rules = parse_rules(rule_version)
    names = _institution_names(db)

    query = db.query(Institution)
    if institution_id is not None:
        query = query.filter(Institution.id == institution_id)
    institutions = query.order_by(Institution.id).all()

    new_task_ids: List[int] = []
    decisions: List[dict] = []

    for inst in institutions:
        operating_status = inst.operating_status or OPERATING_NORMAL
        if operating_status != OPERATING_NORMAL:
            decisions.append(_decision(
                names, inst.id, TaskEventAction.SKIPPED,
                f"机构当前状态为「{operating_status}」，跳过检查任务生成"
            ))
            continue

        score = db.query(ComplianceScore).filter(
            ComplianceScore.institution_id == inst.id
        ).order_by(ComplianceScore.scored_at.desc(), ComplianceScore.id.desc()).first()
        if not score:
            decisions.append(_decision(
                names, inst.id, TaskEventAction.SKIPPED,
                "机构无合规评分记录，无法确定检查频率，跳过生成"
            ))
            continue

        rule = rule_for_grade(rules, score.grade)
        frequency_months = rule["frequency_months"]
        priority = CluePriority(rule["priority"])
        due_date = score.scored_at.date() + timedelta(days=frequency_months * 30)

        if due_date > period_end:
            decisions.append(_decision(
                names, inst.id, TaskEventAction.SKIPPED,
                f"按规则版本{rule_version.version_code}，等级{score.grade.value}对应"
                f"{frequency_months}个月检查频率，应检日期{due_date}超出本周期"
                f"（{period_start}~{period_end}），暂不生成"
            ))
            continue

        # 同一机构同一到期季度只允许存在一个未取消的常规任务
        existing = db.query(InspectionTask).filter(
            InspectionTask.institution_id == inst.id,
            InspectionTask.task_kind == TaskKind.REGULAR,
            InspectionTask.status != TaskStatus.CANCELLED,
        ).all()
        same_quarter = [
            t for t in existing
            if t.due_date and quarter_key(t.due_date) == quarter_key(due_date)
        ]
        if same_quarter:
            keep = same_quarter[0]
            reason = (
                f"机构在{quarter_label(due_date)}已存在未取消的检查任务"
                f"{keep.task_no}（状态：{keep.status.value}），不重复生成"
            )
            decisions.append(_decision(
                names, inst.id, TaskEventAction.KEPT, reason, task=keep
            ))
            _record_event(db, keep, ReplanTrigger.GENERATION,
                          TaskEventAction.KEPT, reason)
            continue

        round_no = db.query(InspectionTask).filter(
            InspectionTask.institution_id == inst.id,
            InspectionTask.task_kind == TaskKind.REGULAR,
        ).count() + 1

        task = InspectionTask(
            institution_id=inst.id,
            compliance_score_id=score.id,
            rule_version_id=rule_version.id,
            task_kind=TaskKind.REGULAR,
            round_no=round_no,
            priority=priority,
            priority_reason=(
                f"按规则版本{rule_version.version_code}，机构等级"
                f"{score.grade.value}（{score.total_score}分，评分#{score.id}）"
                f"对应优先级「{priority.value}」"
            ),
            due_date=due_date,
            status=TaskStatus.PENDING_ASSIGNMENT,
        )
        db.add(task)
        db.flush()
        task.task_no = f"JC{task.id:06d}"
        db.flush()
        new_task_ids.append(task.id)

    return new_task_ids, decisions


def replan_window(
    db: Session,
    window_start: date,
    window_end: date,
    trigger: ReplanTrigger,
    reason: Optional[str] = None,
    pinned: Optional[Dict[int, date]] = None,
    not_before: Optional[Dict[int, date]] = None,
    include_scheduled: bool = True,
    new_task_ids: Optional[Set[int]] = None,
) -> List[dict]:
    """在单个事务内重排窗口内的活动任务。

    步骤：去重（同一机构同一到期季度的重复常规任务，取消多余者）→
    解除范围内任务排期 → 钉住任务优先落位 → 其余任务按
    （优先级、应检日期）顺序占用执法人员剩余容量 → 无法落位的任务
    保留为"待排期"缺口并写明原因。进行中/已完成/已取消任务不参与。
    """
    pinned = pinned or {}
    not_before = not_before or {}
    new_task_ids = new_task_ids or set()
    names = _institution_names(db)
    decisions: List[dict] = []

    active_tasks = db.query(InspectionTask).filter(
        InspectionTask.status.in_(ACTIVE_TASK_STATUSES)
    ).all()

    # ---- 1. 事务内去重：同一机构同一到期季度的重复常规任务 ----
    keep_preferred = set(pinned) | set(not_before)
    groups: Dict[Tuple[int, Tuple[int, int]], List[InspectionTask]] = {}
    for t in active_tasks:
        if t.task_kind != TaskKind.REGULAR or not t.due_date:
            continue
        groups.setdefault((t.institution_id, quarter_key(t.due_date)), []).append(t)

    cancelled_ids: Set[int] = set()
    for (inst_id, qkey), tasks in groups.items():
        if len(tasks) <= 1:
            continue
        tasks.sort(key=lambda t: (
            0 if t.id in keep_preferred else 1,
            PRIORITY_ORDER.get(t.priority, 1),
            t.due_date or date.max,
            t.id,
        ))
        keep = tasks[0]
        for dup in tasks[1:]:
            old_date, old_inspector = dup.scheduled_date, dup.inspector_id
            dup.status = TaskStatus.CANCELLED
            dup.unschedulable_reason = None
            cancel_reason = _compose(
                reason,
                f"同一机构在{quarter_label(dup.due_date)}存在重复检查任务，"
                f"保留任务{keep.task_no}（优先级{keep.priority.value}），取消本任务"
            )
            decisions.append(_decision(
                names, inst_id, TaskEventAction.CANCELLED, cancel_reason,
                task=dup, old_scheduled_date=old_date, old_inspector_id=old_inspector
            ))
            _record_event(
                db, dup, trigger, TaskEventAction.CANCELLED, cancel_reason,
                old_scheduled_date=old_date, old_inspector_id=old_inspector
            )
            cancelled_ids.add(dup.id)
    db.flush()

    # ---- 2. 确定重排范围：窗口内已排期任务 + 全部待排期任务 + 被钉住/约束的任务 ----
    scope: List[InspectionTask] = []
    for t in active_tasks:
        if t.id in cancelled_ids:
            continue
        if t.id in pinned or t.id in not_before:
            scope.append(t)
        elif t.scheduled_date is None:
            scope.append(t)
        elif include_scheduled and window_start <= t.scheduled_date <= window_end:
            scope.append(t)

    old_assignments = {t.id: (t.scheduled_date, t.inspector_id) for t in scope}

    # ---- 3. 占用表：范围外的已排期/进行中任务仍占用容量 ----
    occupancy = _build_occupancy(db, {t.id for t in scope})
    inspectors = _active_inspectors(db)

    # ---- 4. 解除范围内任务排期（仅在内存与事务内，提交前可回滚） ----
    for t in scope:
        t.scheduled_date = None
        t.inspector_id = None
        t.status = TaskStatus.PENDING_ASSIGNMENT

    # ---- 5. 先落位钉住的任务，再按优先级落位其余任务 ----
    pinned_tasks = sorted(
        [t for t in scope if t.id in pinned], key=_task_sort_key
    )
    free_tasks = sorted(
        [t for t in scope if t.id not in pinned], key=_task_sort_key
    )

    for t in pinned_tasks:
        _seat_task(
            t, inspectors, occupancy, window_start, window_end,
            pinned_date=pinned[t.id]
        )
    for t in free_tasks:
        _seat_task(
            t, inspectors, occupancy, window_start, window_end,
            not_before=not_before.get(t.id)
        )

    # ---- 6. 对比新旧排期，生成决策与留痕 ----
    for t in sorted(scope, key=_task_sort_key):
        old_date, old_inspector = old_assignments[t.id]
        is_new = t.id in new_task_ids

        if t.status == TaskStatus.SCHEDULED:
            if old_date == t.scheduled_date and old_inspector == t.inspector_id:
                body = (
                    f"排期不变：{t.scheduled_date}，执法人员#{t.inspector_id}，"
                    f"优先级{t.priority.value}"
                )
                decisions.append(_decision(
                    names, t.institution_id, TaskEventAction.KEPT,
                    _compose(reason, body), task=t,
                    old_scheduled_date=old_date, new_scheduled_date=t.scheduled_date,
                    old_inspector_id=old_inspector, new_inspector_id=t.inspector_id
                ))
                _record_event(
                    db, t, trigger, TaskEventAction.KEPT, _compose(reason, body),
                    old_scheduled_date=old_date, new_scheduled_date=t.scheduled_date,
                    old_inspector_id=old_inspector, new_inspector_id=t.inspector_id
                )
            elif is_new:
                body = (
                    f"新生成检查任务（第{t.round_no}轮），按优先级{t.priority.value}"
                    f"排期至{t.scheduled_date}，执法人员#{t.inspector_id}"
                )
                decisions.append(_decision(
                    names, t.institution_id, TaskEventAction.CREATED,
                    _compose(reason, body), task=t,
                    new_scheduled_date=t.scheduled_date,
                    new_inspector_id=t.inspector_id
                ))
                _record_event(
                    db, t, trigger, TaskEventAction.CREATED, _compose(reason, body),
                    new_scheduled_date=t.scheduled_date,
                    new_inspector_id=t.inspector_id
                )
            elif old_date is None:
                body = (
                    f"待排期缺口补入：排期至{t.scheduled_date}，"
                    f"执法人员#{t.inspector_id}"
                )
                decisions.append(_decision(
                    names, t.institution_id, TaskEventAction.RESCHEDULED,
                    _compose(reason, body), task=t,
                    new_scheduled_date=t.scheduled_date,
                    new_inspector_id=t.inspector_id
                ))
                _record_event(
                    db, t, trigger, TaskEventAction.RESCHEDULED,
                    _compose(reason, body),
                    new_scheduled_date=t.scheduled_date,
                    new_inspector_id=t.inspector_id
                )
            else:
                body = (
                    f"由{old_date}（执法人员#{old_inspector}）调整至"
                    f"{t.scheduled_date}（执法人员#{t.inspector_id}）"
                )
                decisions.append(_decision(
                    names, t.institution_id, TaskEventAction.RESCHEDULED,
                    _compose(reason, body), task=t,
                    old_scheduled_date=old_date, new_scheduled_date=t.scheduled_date,
                    old_inspector_id=old_inspector, new_inspector_id=t.inspector_id
                ))
                _record_event(
                    db, t, trigger, TaskEventAction.RESCHEDULED,
                    _compose(reason, body),
                    old_scheduled_date=old_date, new_scheduled_date=t.scheduled_date,
                    old_inspector_id=old_inspector, new_inspector_id=t.inspector_id
                )
        else:
            # 无法落位：保留为明确的待排期缺口，不静默丢弃
            if not inspectors:
                body = "无可用执法人员，任务保留为待排期缺口"
            elif is_new:
                body = (
                    f"新建任务在窗口{window_start}~{window_end}内执法人员容量不足，"
                    f"保留为待排期缺口"
                )
            elif old_date is not None:
                body = (
                    f"原排期{old_date}因重排失效，窗口{window_start}~{window_end}内"
                    f"执法人员容量不足，保留为待排期缺口"
                )
            else:
                body = (
                    f"窗口{window_start}~{window_end}内执法人员容量不足，"
                    f"任务保持待排期缺口"
                )
            if t.id in pinned:
                body = (
                    f"指定日期{pinned[t.id]}执法人员容量已满，"
                    f"任务保留为待排期缺口"
                )
            t.unschedulable_reason = _compose(reason, body)
            decisions.append(_decision(
                names, t.institution_id, TaskEventAction.GAP,
                t.unschedulable_reason, task=t,
                old_scheduled_date=old_date, old_inspector_id=old_inspector
            ))
            _record_event(
                db, t, trigger, TaskEventAction.GAP, t.unschedulable_reason,
                old_scheduled_date=old_date, old_inspector_id=old_inspector
            )

    db.flush()
    return decisions


def generate_tasks(
    db: Session,
    period_start: date,
    period_end: date,
    institution_id: Optional[int] = None,
    rule_version_id: Optional[int] = None,
) -> Tuple[List[dict], RuleVersion]:
    """生成季度检查任务：固定评分依据与规则版本，按容量约束排期。"""
    if rule_version_id is not None:
        rule_version = db.query(RuleVersion).filter(
            RuleVersion.id == rule_version_id
        ).first()
        if not rule_version:
            raise HTTPException(status_code=404, detail="规则版本不存在")
    else:
        rule_version = get_active_rule_version(db)
        if not rule_version:
            raise HTTPException(
                status_code=400,
                detail="不存在启用的频率规则版本，请先创建规则版本"
            )

    new_task_ids, pre_decisions = create_task_candidates(
        db, period_start, period_end, rule_version, institution_id
    )
    decisions = replan_window(
        db, period_start, period_end,
        trigger=ReplanTrigger.GENERATION,
        include_scheduled=False,
        new_task_ids=set(new_task_ids),
    )
    return pre_decisions + decisions, rule_version


def validate_consistency(db: Session) -> None:
    """提交前的一致性校验：容量约束与重复任务约束，违反则抛错触发回滚。"""
    scheduled = db.query(InspectionTask).filter(
        InspectionTask.status.in_([TaskStatus.SCHEDULED, TaskStatus.IN_PROGRESS]),
        InspectionTask.scheduled_date.isnot(None),
        InspectionTask.inspector_id.isnot(None),
    ).all()
    loads: Dict[Tuple[int, date], int] = {}
    for t in scheduled:
        key = (t.inspector_id, t.scheduled_date)
        loads[key] = loads.get(key, 0) + 1
    inspectors = {i.id: i for i in db.query(Inspector).all()}
    for (inspector_id, day), count in loads.items():
        inspector = inspectors.get(inspector_id)
        if inspector and count > inspector.daily_capacity:
            raise ValueError(
                f"执法人员{inspector.name}在{day}的任务数{count}"
                f"超过日容量{inspector.daily_capacity}"
            )

    active_regular = db.query(InspectionTask).filter(
        InspectionTask.status.in_(ACTIVE_TASK_STATUSES),
        InspectionTask.task_kind == TaskKind.REGULAR,
    ).all()
    seen: Dict[Tuple[int, Tuple[int, int]], int] = {}
    for t in active_regular:
        if not t.due_date:
            continue
        key = (t.institution_id, quarter_key(t.due_date))
        if key in seen:
            raise ValueError(
                f"机构#{t.institution_id}在{quarter_label(t.due_date)}"
                f"存在多个活动的常规检查任务"
            )
        seen[key] = t.id

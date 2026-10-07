"""
有容量约束的检查任务服务。

职责：
1. 季度计划生成：为每个机构按其最新评分（连同冻结的规则版本）生成检查任务，
   任务上固化评分、等级、频率、优先级快照；在统一事务内按优先级与风险高低
   排序，占用执法人员的日容量完成排期，排不进窗口的任务以 UNSCHEDULED 状态
   + ScheduleGap 显式保留。
2. 事件驱动重算：延期、机构停业、风险突升、检查结果回写、规则换版、批量重排
   都会在单事务内决定每个任务应保留、取消还是重排，并写入 TaskChangeLog
   说明原因（含优先级变化原因）。
3. 不可变性：已完成/进行中任务不允许被新规则或任何重排改写，尝试时抛出
   TaskLockedError，由路由转为 409。
4. 冲突处理：同一机构的重复开放任务在应用层拒绝、并用数据库部分唯一索引
   兜底；执法人员同一日期的任务数不得超过其日容量（请假日期不可排）。
"""
import uuid
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy.orm import Session

from .models import (
    Institution, ComplianceScore, ComplianceGrade,
    CluePriority, RuleVersion, Inspector, InspectorBlock,
    InspectionTask, TaskStatus, TaskChangeType, ChangeTrigger,
    ScheduleGap, GapReason, TaskChangeLog,
)
from . import rule_engine as rules_mod


TERMINAL_STATUSES = {TaskStatus.COMPLETED, TaskStatus.CANCELLED}
LOCKED_STATUSES = {TaskStatus.COMPLETED, TaskStatus.IN_PROGRESS}
OPEN_STATUSES = {
    TaskStatus.SCHEDULED, TaskStatus.POSTPONED, TaskStatus.UNSCHEDULED
}

PRIORITY_RANK = {CluePriority.HIGH: 0, CluePriority.MEDIUM: 1, CluePriority.LOW: 2}


class TaskLockedError(Exception):
    """已执行（完成/进行中）的任务不能被改写。"""


class DuplicateTaskError(Exception):
    """同一机构、同一季度、同一轮次已存在未终结任务。"""


class SchedulingError(Exception):
    """排期前置条件不满足（如无可用执法人员）。"""


def new_batch_id() -> str:
    return uuid.uuid4().hex[:16]


def quarter_label(d: date) -> str:
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


# ---------------------------------------------------------------------------
# 审计日志
# ---------------------------------------------------------------------------

def _log(
    db: Session, task: InspectionTask, change_type: TaskChangeType,
    trigger: ChangeTrigger, reason: str, *,
    old_status=None, new_status=None,
    old_priority=None, new_priority=None,
    old_date=None, new_date=None,
    old_inspector_id=None, new_inspector_id=None,
    batch_id: Optional[str] = None,
):
    db.add(TaskChangeLog(
        task_id=task.id,
        change_type=change_type,
        trigger=trigger,
        reason=reason,
        old_status=old_status,
        new_status=new_status,
        old_priority=old_priority,
        new_priority=new_priority,
        old_scheduled_date=old_date,
        new_scheduled_date=new_date,
        old_inspector_id=old_inspector_id,
        new_inspector_id=new_inspector_id,
        batch_id=batch_id,
    ))


# ---------------------------------------------------------------------------
# 容量视图：在单事务/单会话内维护 执法人员->日期->已占用容量
# ---------------------------------------------------------------------------

class CapacityView:
    def __init__(self, db: Session):
        self.db = db
        # (inspector_id, day) -> 已占用任务数
        self.used: Dict[tuple, int] = {}
        blocked_dates = {(b.inspector_id, b.block_date) for b in db.query(InspectorBlock).all()}
        self.blocked = blocked_dates
        self.inspectors = db.query(Inspector).filter(Inspector.is_active == True).all()  # noqa: E712
        for task in db.query(InspectionTask).filter(
            InspectionTask.status.in_([TaskStatus.SCHEDULED, TaskStatus.IN_PROGRESS, TaskStatus.POSTPONED])
        ).all():
            if task.scheduled_date and task.inspector_id is not None:
                key = (task.inspector_id, task.scheduled_date)
                self.used[key] = self.used.get(key, 0) + 1

    def _available_on(self, inspector: Inspector, day: date) -> int:
        if (inspector.id, day) in self.blocked:
            return 0
        return inspector.daily_capacity - self.used.get((inspector.id, day), 0)

    def find_slot(self, window_start: date, window_end: date,
                  exclude_inspector_id: Optional[int] = None):
        """在窗口内贪心寻找最早可排日期与执法人员。

        优先占用当日剩余容量最少的执法人员（尽量装满再开新人/新日），
        同日多人时按人员创建顺序保持确定性。返回 (day, inspector) 或 None。
        """
        if not self.inspectors:
            return None
        day = window_start
        while day <= window_end:
            best = None
            best_remaining = None
            for inspector in self.inspectors:
                if exclude_inspector_id is not None and inspector.id == exclude_inspector_id:
                    continue
                remaining = self._available_on(inspector, day)
                if remaining <= 0:
                    continue
                if best is None or remaining < best_remaining or (
                    remaining == best_remaining and inspector.id < best.id
                ):
                    best, best_remaining = inspector, remaining
            if best is not None:
                return day, best
            day += timedelta(days=1)
        return None

    def occupy(self, inspector: Inspector, day: date):
        key = (inspector.id, day)
        self.used[key] = self.used.get(key, 0) + 1

    def release(self, inspector_id: int, day: date):
        key = (inspector_id, day)
        if key in self.used and self.used[key] > 0:
            self.used[key] -= 1


# ---------------------------------------------------------------------------
# 任务生成
# ---------------------------------------------------------------------------

def _focus_content(grade: ComplianceGrade):
    focus_areas_map = {
        ComplianceGrade.EXCELLENT: "常规合规检查，重点关注执业资质维护",
        ComplianceGrade.GOOD: "常规合规检查，重点关注执业规范性",
        ComplianceGrade.FAIR: "重点检查超范围执业、人员资质问题，核查整改落实情况",
        ComplianceGrade.POOR: "全面执法检查，重点核查无证上岗、超范围执业、虚假宣传等严重违规行为",
    }
    content_template = {
        ComplianceGrade.EXCELLENT: [
            "1. 检查医疗机构执业许可证有效性及诊疗科目范围",
            "2. 抽查从业人员执业资质",
            "3. 检查医疗广告发布情况"
        ],
        ComplianceGrade.GOOD: [
            "1. 检查医疗机构执业许可证及诊疗科目",
            "2. 核查从业人员执业资质，重点抽查医师、护士",
            "3. 检查医疗质量安全管理制度落实情况",
            "4. 抽查近期开展的医疗美容项目是否合规"
        ],
        ComplianceGrade.FAIR: [
            "1. 全面核查医疗机构执业许可证及诊疗项目授权",
            "2. 逐一核查所有从业人员执业资质",
            "3. 检查近6个月所有诊疗记录，排查超范围执业情况",
            "4. 核查广告宣传内容真实性",
            "5. 检查前期问题整改落实情况"
        ],
        ComplianceGrade.POOR: [
            "1. 全面执法检查，核查所有执业资质",
            "2. 逐一核查所有从业人员资质，严禁无证上岗",
            "3. 检查近12个月所有诊疗记录，逐一核实项目合规性",
            "4. 全面排查虚假宣传线索，包括线上线下广告",
            "5. 核查所有已核实违规问题的整改情况",
            "6. 依法查处发现的违法违规行为"
        ],
    }
    return focus_areas_map[grade], "\n".join(content_template[grade])


def build_tasks_for_score(
    db: Session, score: ComplianceScore, rule_version: RuleVersion,
    rules: dict, *, start_date: date, rounds: Optional[int] = None,
    batch_id: str,
) -> List[InspectionTask]:
    """按冻结评分与规则构建任务对象（不落库、不排期），并拒绝重复任务。"""
    institution = db.query(Institution).get(score.institution_id)
    grade = score.grade
    frequency = score.inspection_frequency
    priority = rules_mod.priority_of(rules, grade)
    months = rules_mod.frequency_months_of(rules, frequency)
    window_days = rules_mod.window_days_of(rules)
    if rounds is None:
        rounds = rules_mod.rounds_of(rules, grade)

    focus, content = _focus_content(grade)
    grade_name = grade.value
    tasks: List[InspectionTask] = []

    for i in range(rounds):
        earliest = start_date + timedelta(days=months * 30 * i)
        due = earliest + timedelta(days=window_days)
        qlabel = quarter_label(earliest)

        existing = db.query(InspectionTask).filter(
            InspectionTask.institution_id == institution.id,
            InspectionTask.quarter == qlabel,
            InspectionTask.round_no == i + 1,
            InspectionTask.status.notin_(list(TERMINAL_STATUSES)),
        ).first()
        if existing:
            raise DuplicateTaskError(
                f"机构 {institution.name} 在 {qlabel} 第{i+1}轮已存在未终结任务（任务#{existing.id}），"
                f"不得重复生成"
            )

        task = InspectionTask(
            institution_id=institution.id,
            compliance_score_id=score.id,
            rule_version_id=rule_version.id,
            round_no=i + 1,
            quarter=qlabel,
            frozen_score=score.total_score,
            frozen_grade=grade,
            frozen_frequency=frequency,
            priority=priority,
            title=f"[{grade_name}] {institution.name} {qlabel} 第{i+1}轮监管检查任务",
            plan_content=content,
            focus_areas=focus,
            earliest_date=earliest,
            due_date=due,
            status=TaskStatus.UNSCHEDULED,
        )
        db.add(task)
        db.flush()
        _log(db, task, TaskChangeType.CREATED, ChangeTrigger.PLAN_GENERATION,
             f"季度计划生成：依据评分 {score.total_score} 分（{grade_name}级）、"
             f"规则版本 {rule_version.version_code}、检查频率 {frequency.value} 创建任务",
             new_status=TaskStatus.UNSCHEDULED, new_priority=priority, batch_id=batch_id)
        tasks.append(task)

    return tasks


def _open_gap(db: Session, task: InspectionTask, reason: GapReason, detail: str,
              window_start: date, window_end: date, batch_id: str,
              trigger: ChangeTrigger = ChangeTrigger.BATCH_RESCHEDULE):
    gap = task.schedule_gap
    if gap is None:
        gap = ScheduleGap(
            task_id=task.id,
            institution_id=task.institution_id,
            reason=reason,
            detail=detail,
            window_start=window_start,
            window_end=window_end,
            required_capacity=1,
            batch_id=batch_id,
        )
        db.add(gap)
        _log(db, task, TaskChangeType.GAP_OPENED, trigger,
             f"产生排期缺口：{detail}", batch_id=batch_id)
    else:
        gap.reason = reason
        gap.detail = detail
        gap.window_start = window_start
        gap.window_end = window_end
        gap.resolved = False
        gap.resolved_at = None
        gap.batch_id = batch_id
    db.flush()
    return gap


def _resolve_gap(db: Session, task: InspectionTask, batch_id: Optional[str], trigger: ChangeTrigger):
    gap = task.schedule_gap
    if gap is not None and not gap.resolved:
        gap.resolved = True
        gap.resolved_at = datetime.utcnow()
        _log(db, task, TaskChangeType.GAP_RESOLVED, trigger,
             f"排期缺口已消除，任务排入 {task.scheduled_date}", batch_id=batch_id)


def _assign_slot(db: Session, task: InspectionTask, day: date,
                 inspector: Inspector, capacity: CapacityView,
                 trigger: ChangeTrigger, reason: str, batch_id: Optional[str],
                 change_type: TaskChangeType):
    old_date, old_inspector, old_status = task.scheduled_date, task.inspector_id, task.status
    task.scheduled_date = day
    task.inspector_id = inspector.id
    task.status = TaskStatus.SCHEDULED
    capacity.occupy(inspector, day)
    _log(db, task, change_type, trigger, reason,
         old_status=old_status,
         new_status=TaskStatus.SCHEDULED,
         old_date=old_date, new_date=day,
         old_inspector_id=old_inspector, new_inspector_id=inspector.id,
         batch_id=batch_id)
    _resolve_gap(db, task, batch_id, trigger)


def generate_quarterly_plan(
    db: Session,
    *,
    start_date: Optional[date] = None,
    institution_ids: Optional[List[int]] = None,
    rule_version_id: Optional[int] = None,
    commit: bool = True,
) -> dict:
    """为一批机构生成季度检查任务并在同一事务内完成容量约束排期。

    高风险（评分低/优先级高）的任务先占容量，避免高风险机构被排到后面。
    排不进窗口的任务保留为 UNSCHEDULED 并登记 ScheduleGap，绝不静默丢失。
    """
    start_date = start_date or date.today()
    batch_id = new_batch_id()

    rule_version = rules_mod.get_rule_version(db, rule_version_id)
    rules = rules_mod.load_rules(rule_version)

    query = db.query(ComplianceScore)
    if institution_ids is not None:
        query = query.filter(ComplianceScore.institution_id.in_(institution_ids))
    latest_scores = _latest_scores(query)

    # 确保此前会话内的待写入数据对重复任务预检可见
    db.flush()
    capacity = CapacityView(db)
    created: List[InspectionTask] = []
    skipped_duplicates: List[dict] = []
    skipped_suspended: List[dict] = []

    # 先为所有机构构建任务（重复任务/停业机构在此处显式跳过并说明）
    for score in latest_scores:
        institution = db.query(Institution).get(score.institution_id)
        if institution is None:
            continue
        if institution.is_suspended:
            skipped_suspended.append({
                "institution_id": institution.id,
                "institution_name": institution.name,
                "reason": "机构处于停业状态，本季度不生成检查任务",
            })
            continue
        try:
            tasks = build_tasks_for_score(
                db, score, rule_version, rules,
                start_date=start_date, batch_id=batch_id,
            )
        except DuplicateTaskError as exc:
            skipped_duplicates.append({
                "institution_id": score.institution_id,
                "institution_name": institution.name,
                "reason": str(exc),
            })
            continue
        created.extend(tasks)

    # 全局排序后统一排期：优先级高者优先，同优先级按风险评分（低分为高风险），
    # 再按最早应检日期保证确定性
    ordered = sorted(
        created,
        key=lambda t: (PRIORITY_RANK[t.priority], t.frozen_score, t.earliest_date, t.id),
    )

    scheduled: List[InspectionTask] = []
    gaps: List[ScheduleGap] = []
    no_inspectors = not capacity.inspectors
    for task in ordered:
        slot = capacity.find_slot(task.earliest_date, task.due_date)
        if slot is None:
            detail = (
                "系统中尚无可用执法人员，任务无法排期"
                if no_inspectors else
                f"排期窗口 {task.earliest_date} ~ {task.due_date} 内执法人员容量已满或时间冲突"
            )
            reason = GapReason.INSUFFICIENT_INSPECTORS if no_inspectors else GapReason.NO_CAPACITY
            task.status = TaskStatus.UNSCHEDULED
            gap = _open_gap(db, task, reason, detail,
                            task.earliest_date, task.due_date, batch_id,
                            trigger=ChangeTrigger.PLAN_GENERATION)
            gaps.append(gap)
            continue
        day, inspector = slot
        _assign_slot(
            db, task, day, inspector, capacity,
            ChangeTrigger.PLAN_GENERATION,
            f"季度计划排期：按优先级 {task.priority.value} / 风险评分 {task.frozen_score}"
            f" 占用 {inspector.name} {day} 的执法容量",
            batch_id, TaskChangeType.SCHEDULED,
        )
        scheduled.append(task)

    if commit:
        db.commit()
        for task in created:
            db.refresh(task)

    return {
        "batch_id": batch_id,
        "rule_version_code": rule_version.version_code,
        "quarter": quarter_label(start_date),
        "tasks_created": len(created),
        "tasks_scheduled": len(scheduled),
        "gaps": gaps,
        "skipped_duplicates": skipped_duplicates,
        "skipped_suspended": skipped_suspended,
        "tasks": created,
    }


def _latest_scores(query) -> List[ComplianceScore]:
    """取每个机构最新的一条评分记录。"""
    results: Dict[int, ComplianceScore] = {}
    for score in query.order_by(ComplianceScore.scored_at.asc(), ComplianceScore.id.asc()).all():
        results[score.institution_id] = score
    return list(results.values())


# ---------------------------------------------------------------------------
# 单任务排期 / 重排（事件处理器共用）
# ---------------------------------------------------------------------------

def reschedule_task(
    db: Session, task: InspectionTask, *,
    window_start: date, window_end: date,
    trigger: ChangeTrigger, reason: str,
    batch_id: Optional[str] = None,
    commit: bool = False,
    exclude_inspector_id: Optional[int] = None,
) -> Optional[ScheduleGap]:
    """尝试把任务重排到窗口内；成功返回 None，失败返回/更新缺口。

    重排前释放原日期占用，使腾挪出的容量可被复用；找不到新槽位时，旧槽位
    同样释放并保留为 UNSCHEDULED + ScheduleGap 明确缺口（不静默保留旧日期，
    以免容量视图与任务状态不一致）。已完成/进行中任务抛 TaskLockedError。
    """
    if task.status in LOCKED_STATUSES:
        raise TaskLockedError(f"任务 #{task.id} 已处于{task.status.value}状态，不能重排")

    # 容量视图直接查库，先把同事务内此前的改动落盘到事务中（autoflush=False）
    db.flush()
    capacity = CapacityView(db)
    old_date, old_inspector_id = task.scheduled_date, task.inspector_id
    if old_date and old_inspector_id is not None:
        capacity.release(old_inspector_id, old_date)

    slot = capacity.find_slot(window_start, window_end, exclude_inspector_id)
    if slot is None:
        no_inspectors = not capacity.inspectors
        detail = (
            f"{reason}；但系统中无可用执法人员"
            if no_inspectors else
            f"{reason}；窗口 {window_start} ~ {window_end} 内无可用执法容量"
        )
        task.scheduled_date = None
        task.inspector_id = None
        task.status = TaskStatus.UNSCHEDULED
        gap = _open_gap(
            db, task,
            GapReason.INSUFFICIENT_INSPECTORS if no_inspectors else GapReason.NO_CAPACITY,
            detail, window_start, window_end, batch_id or new_batch_id(),
            trigger=trigger,
        )
        _log(db, task, TaskChangeType.RESCHEDULED, trigger, detail,
             old_date=old_date, new_status=TaskStatus.UNSCHEDULED,
             old_inspector_id=old_inspector_id, batch_id=batch_id)
        if commit:
            db.commit()
        return gap

    day, inspector = slot
    task.scheduled_date = day
    task.inspector_id = inspector.id
    task.status = TaskStatus.SCHEDULED
    _log(db, task, TaskChangeType.RESCHEDULED, trigger, reason,
         new_status=TaskStatus.SCHEDULED,
         old_date=old_date, new_date=day,
         old_inspector_id=old_inspector_id, new_inspector_id=inspector.id,
         batch_id=batch_id)
    _resolve_gap(db, task, batch_id, trigger)
    if commit:
        db.commit()
        db.refresh(task)
    return None


# ---------------------------------------------------------------------------
# 事件：检查延期
# ---------------------------------------------------------------------------

def postpone_task(
    db: Session, task_id: int, *,
    new_date: Optional[date] = None,
    reason: str = "",
    commit: bool = True,
) -> InspectionTask:
    """延期单个任务：释放原日期容量并在事务内重新排期。"""
    task = db.query(InspectionTask).get(task_id)
    if task is None:
        raise ValueError("任务不存在")
    if task.status in LOCKED_STATUSES:
        raise TaskLockedError(f"任务 #{task.id} 已处于{task.status.value}状态，不能延期")
    if task.status == TaskStatus.CANCELLED:
        raise TaskLockedError(f"任务 #{task.id} 已取消，不能延期")

    old_date = task.scheduled_date
    trigger_reason = f"检查延期：{reason or '执法人员/机构申请改期'}"

    task.status = TaskStatus.POSTPONED
    _log(db, task, TaskChangeType.POSTPONED, ChangeTrigger.POSTPONE, trigger_reason,
         old_status=TaskStatus.SCHEDULED if old_date else TaskStatus.UNSCHEDULED,
         new_status=TaskStatus.POSTPONED,
         old_date=old_date, batch_id=None)

    # 延期后的重排窗口：从新日期起一个规则窗口长度
    rules = rules_mod.load_rules(task.rule_version)
    window_days = rules_mod.window_days_of(rules)
    window_start = new_date or (old_date or date.today())
    window_end = window_start + timedelta(days=window_days)

    reschedule_task(
        db, task,
        window_start=window_start, window_end=window_end,
        trigger=ChangeTrigger.POSTPONE,
        reason=f"{trigger_reason}，尝试在 {window_start} ~ {window_end} 内重新安排有限执法资源",
        commit=False,
    )

    # 延期释放出的容量在同一事务内重新分配给其他未执行任务（全局重排，
    # 排除本任务以保留其新确定的槽位）
    batch_reschedule(
        db,
        trigger=ChangeTrigger.POSTPONE,
        reason=f"任务#{task.id}延期后，重新计算并安排其他未执行任务",
        commit=False,
        exclude_task_ids=[task.id],
    )

    if commit:
        db.commit()
        db.refresh(task)
    return task


# ---------------------------------------------------------------------------
# 事件：机构停业 / 复业
# ---------------------------------------------------------------------------

def suspend_institution(
    db: Session, institution_id: int, *,
    reason: str, expected_resume_date: Optional[date] = None,
    commit: bool = True,
) -> dict:
    """机构停业：取消其全部未执行任务（保留审计轨迹与缺口说明）。"""
    institution = db.query(Institution).get(institution_id)
    if institution is None:
        raise ValueError("机构不存在")

    institution.is_suspended = True
    institution.suspended_at = datetime.utcnow()
    institution.suspend_reason = reason
    institution.expected_resume_date = expected_resume_date

    open_tasks = db.query(InspectionTask).filter(
        InspectionTask.institution_id == institution_id,
        InspectionTask.status.in_(list(OPEN_STATUSES)),
    ).all()

    cancelled = []
    for task in open_tasks:
        old_status, old_date = task.status, task.scheduled_date
        task.status = TaskStatus.CANCELLED
        task.scheduled_date = None
        task.inspector_id = None
        _log(db, task, TaskChangeType.CANCELLED, ChangeTrigger.SUSPENSION,
             f"机构停业（{reason}），取消未执行任务；已完成/进行中任务不受影响",
             old_status=old_status, new_status=TaskStatus.CANCELLED,
             old_date=old_date)
        if task.schedule_gap is not None and not task.schedule_gap.resolved:
            task.schedule_gap.resolved = True
            task.schedule_gap.resolved_at = datetime.utcnow()
        cancelled.append(task)

    locked_tasks = db.query(InspectionTask).filter(
        InspectionTask.institution_id == institution_id,
        InspectionTask.status.in_(list(LOCKED_STATUSES)),
    ).all()
    locked = [{"task_id": t.id, "status": t.status.value} for t in locked_tasks]

    if commit:
        db.commit()
    return {
        "institution_id": institution_id,
        "suspended": True,
        "cancelled_task_ids": [t.id for t in cancelled],
        "locked_tasks_kept": locked,
    }


def resume_institution(db: Session, institution_id: int, *, commit: bool = True) -> dict:
    """机构复业：解除停业标记（未完成任务需另行通过批量重排重新安排）。"""
    institution = db.query(Institution).get(institution_id)
    if institution is None:
        raise ValueError("机构不存在")
    institution.is_suspended = False
    institution.suspended_at = None
    institution.suspend_reason = None
    institution.expected_resume_date = None
    if commit:
        db.commit()
    return {"institution_id": institution_id, "suspended": False}


# ---------------------------------------------------------------------------
# 事件：风险突升
# ---------------------------------------------------------------------------

def handle_risk_surge(
    db: Session, institution_id: int, *,
    reason: str,
    new_score: Optional[ComplianceScore] = None,
    commit: bool = True,
) -> dict:
    """机构风险突升：未执行任务提级到高优先级，并在容量允许时尽量提前排期。

    可传入新的评分记录（任务的后续生成将采用它）；已生成任务上冻结的评分
    快照不被改写，但优先级与排期位置按事件调整并全程记录原因。
    已完成/进行中任务保持不动。
    """
    institution = db.query(Institution).get(institution_id)
    if institution is None:
        raise ValueError("机构不存在")

    score = new_score or db.query(ComplianceScore).filter(
        ComplianceScore.institution_id == institution_id
    ).order_by(ComplianceScore.scored_at.desc(), ComplianceScore.id.desc()).first()
    if score is None:
        raise SchedulingError("该机构尚无评分记录，无法处理风险突升")

    tasks = db.query(InspectionTask).filter(
        InspectionTask.institution_id == institution_id,
        InspectionTask.status.in_(list(OPEN_STATUSES)),
    ).order_by(InspectionTask.due_date.asc()).all()

    upgraded, rescheduled, gaps = [], [], []
    for task in tasks:
        old_priority = task.priority
        if old_priority != CluePriority.HIGH:
            task.priority = CluePriority.HIGH
            _log(db, task, TaskChangeType.PRIORITY_CHANGED, ChangeTrigger.RISK_SURGE,
                 f"风险突升（{reason}）：优先级由 {old_priority.value} 提升为高，"
                 f"在排队序列中前移",
                 old_priority=old_priority, new_priority=CluePriority.HIGH)
        upgraded.append(task)

        # 风险突升任务尝试从今天起提前安排（不早于原最早应检日之前无意义，
        # 但风险突升属于加急，允许从今日起插入容量空档）
        window_start = date.today()
        window_end = task.due_date
        if window_end < window_start:
            window_end = window_start + timedelta(
                days=rules_mod.window_days_of(rules_mod.load_rules(task.rule_version)))
        gap = reschedule_task(
            db, task, window_start=window_start, window_end=window_end,
            trigger=ChangeTrigger.RISK_SURGE,
            reason=f"风险突升（{reason}），高优先级任务尝试提前排期",
        )
        if gap is None:
            rescheduled.append(task)
        else:
            gaps.append(gap)

    if commit:
        db.commit()
        for t in upgraded:
            db.refresh(t)
    return {
        "institution_id": institution_id,
        "upgraded_task_ids": [t.id for t in upgraded],
        "rescheduled_task_ids": [t.id for t in rescheduled],
        "gap_task_ids": [g.task_id for g in gaps],
    }


# ---------------------------------------------------------------------------
# 事件：检查结果回写
# ---------------------------------------------------------------------------

def mark_task_in_progress(
    db: Session, task_id: int, *, commit: bool = True
) -> InspectionTask:
    """任务开始执行：进入进行中状态，此后不可被重排或新规则改写。"""
    task = db.query(InspectionTask).get(task_id)
    if task is None:
        raise ValueError("任务不存在")
    if task.status in LOCKED_STATUSES:
        raise TaskLockedError(f"任务 #{task.id} 已处于{task.status.value}状态")
    if task.status == TaskStatus.CANCELLED:
        raise TaskLockedError(f"任务 #{task.id} 已取消，不能开始执行")
    old_status = task.status
    task.status = TaskStatus.IN_PROGRESS
    _log(db, task, TaskChangeType.STARTED, ChangeTrigger.MANUAL,
         "任务开始执行，进入进行中状态，排期与评分依据锁定",
         old_status=old_status, new_status=TaskStatus.IN_PROGRESS)
    if commit:
        db.commit()
        db.refresh(task)
    return task


def record_task_result(
    db: Session, task_id: int, *,
    result: str,
    inspection_date: Optional[date] = None,
    commit: bool = True,
) -> InspectionTask:
    """回写检查结果：任务终结为已完成，此后不可被任何新规则/重排改写。"""
    task = db.query(InspectionTask).get(task_id)
    if task is None:
        raise ValueError("任务不存在")
    if task.status == TaskStatus.COMPLETED:
        raise TaskLockedError(f"任务 #{task.id} 已完成，结果不能重复回写")
    if task.status == TaskStatus.CANCELLED:
        raise TaskLockedError(f"任务 #{task.id} 已取消，不能回写结果")

    inspection_date = inspection_date or task.scheduled_date or date.today()
    old_status = task.status
    task.actual_inspection_date = inspection_date
    task.result = result
    task.status = TaskStatus.COMPLETED

    _log(db, task, TaskChangeType.RESULT_RECORDED, ChangeTrigger.RESULT_WRITEBACK,
         f"检查结果回写：{result}",
         old_status=old_status, new_status=TaskStatus.COMPLETED)

    # 检查完成释放出的执法容量在同一事务内重新分配给其他未执行任务（全局重排）
    batch_reschedule(
        db,
        trigger=ChangeTrigger.RESULT_WRITEBACK,
        reason=f"任务#{task.id}检查结果回写并完成，重新计算并安排其他未执行任务",
        commit=False,
    )

    if commit:
        db.commit()
        db.refresh(task)
    return task


# ---------------------------------------------------------------------------
# 批量重排
# ---------------------------------------------------------------------------

def batch_reschedule(
    db: Session, *,
    from_date: Optional[date] = None,
    institution_ids: Optional[List[int]] = None,
    trigger: ChangeTrigger = ChangeTrigger.BATCH_RESCHEDULE,
    reason: str = "批量重排未执行任务",
    commit: bool = True,
    exclude_task_ids: Optional[List[int]] = None,
) -> dict:
    """在单事务内重排所有未执行任务。

    按当前优先级与冻结风险评分重新竞争容量；高风险先排。个别任务无法安排时
    保留 UNSCHEDULED + ScheduleGap 缺口，不影响其他任务，也绝不静默丢失。
    返回每个任务的 保留/重排/缺口 明细。已完成/进行中任务原样保留。
    exclude_task_ids 中的任务保留其当前槽位，不参与重排（但仍占用容量）。
    """
    from_date = from_date or date.today()
    batch_id = new_batch_id()
    exclude_task_ids = set(exclude_task_ids or [])

    # 查询前必须先把同事务内的状态变化落盘到事务中（本系统 autoflush=False），
    # 否则刚完成/取消的任务会以旧状态被查出并被错误重排
    db.flush()

    tasks_q = db.query(InspectionTask).filter(
        InspectionTask.status.in_(list(OPEN_STATUSES)),
    )
    if institution_ids is not None:
        tasks_q = tasks_q.filter(InspectionTask.institution_id.in_(institution_ids))
    tasks = [t for t in tasks_q.all() if t.id not in exclude_task_ids]

    # 排除停业机构：其开放任务理论上已取消，这里双保险跳过
    tasks = [t for t in tasks if not (t.institution and t.institution.is_suspended)]

    # 快照旧状态用于判定"保留/重排"
    old_slots = {t.id: (t.scheduled_date, t.inspector_id, t.status, t.priority) for t in tasks}

    # 全部释放回容量池（仅内存视图层面），再按优先级重新竞争
    capacity = CapacityView(db)
    for t in tasks:
        if t.scheduled_date and t.inspector_id is not None:
            capacity.release(t.inspector_id, t.scheduled_date)
        t.scheduled_date = None
        t.inspector_id = None
        t.status = TaskStatus.UNSCHEDULED

    ordered = sorted(
        tasks,
        key=lambda t: (PRIORITY_RANK[t.priority], t.frozen_score, t.earliest_date, t.id),
    )

    kept, moved, gaps = [], [], []
    for task in ordered:
        window_start = max(task.earliest_date, from_date)
        window_end = max(task.due_date, window_start + timedelta(
            days=rules_mod.window_days_of(rules_mod.load_rules(task.rule_version))))
        slot = capacity.find_slot(window_start, window_end)
        if slot is None:
            detail = f"{reason}：窗口 {window_start} ~ {window_end} 内执法人员容量不足，任务暂时无法安排"
            gap = _open_gap(db, task, GapReason.NO_CAPACITY, detail,
                            window_start, window_end, batch_id)
            gaps.append(gap)
            continue
        day, inspector = slot
        task.scheduled_date = day
        task.inspector_id = inspector.id
        task.status = TaskStatus.SCHEDULED
        capacity.occupy(inspector, day)

        old_date, old_ins, old_status, old_priority = old_slots[task.id]
        if old_date == day and old_ins == inspector.id:
            _log(db, task, TaskChangeType.KEPT, trigger,
                 f"{reason}：该任务日期/执法人员不变，予以保留",
                 new_status=TaskStatus.SCHEDULED, batch_id=batch_id)
            kept.append(task)
        else:
            _log(db, task, TaskChangeType.RESCHEDULED, trigger,
                 f"{reason}：按优先级 {task.priority.value} / 风险评分 {task.frozen_score} "
                 f"重新竞争容量，{old_date} -> {day}",
                 old_status=old_status, new_status=TaskStatus.SCHEDULED,
                 old_date=old_date, new_date=day,
                 old_inspector_id=old_ins, new_inspector_id=inspector.id,
                 batch_id=batch_id)
            moved.append(task)
        _resolve_gap(db, task, batch_id, trigger)

    # 已完成/进行中任务：原样保留并记录（仅当本次显式要求审计时；为避免噪声，
    # 只在结果中统计，不逐条写日志）
    locked_q = db.query(InspectionTask).filter(
        InspectionTask.status.in_(list(LOCKED_STATUSES))
    )
    if institution_ids is not None:
        locked_q = locked_q.filter(InspectionTask.institution_id.in_(institution_ids))
    locked_count = locked_q.count()

    if commit:
        db.commit()
        for t in kept + moved:
            db.refresh(t)

    return {
        "batch_id": batch_id,
        "reason": reason,
        "kept": kept,
        "rescheduled": moved,
        "gaps": gaps,
        "locked_tasks_untouched": locked_count,
    }


# ---------------------------------------------------------------------------
# 规则换版：冻结旧任务，新口径只影响换版后新生成的任务
# ---------------------------------------------------------------------------

def upgrade_rule_version(
    db: Session, *,
    version_code: str,
    effective_from: date,
    change_summary: str,
    rules: Optional[dict] = None,
    commit: bool = True,
) -> dict:
    """创建新规则版本。已存在的任务与其冻结快照不受影响。

    同时说明：当前开放任务是否需要按新口径调整由调用方决定——默认不改写，
    以保证"已经安排的检查依据"可追溯；如确需让新频率影响未来轮次，应重新
    生成季度计划。
    """
    new_version = rules_mod.create_rule_version(
        db,
        version_code=version_code,
        effective_from=effective_from,
        change_summary=change_summary,
        rules=rules,
        flush_only=True,
    )
    open_count = db.query(InspectionTask).filter(
        InspectionTask.status.in_(list(OPEN_STATUSES))
    ).count()
    if commit:
        db.commit()
        db.refresh(new_version)
    return {
        "rule_version_id": new_version.id,
        "version_code": new_version.version_code,
        "open_tasks_unchanged": open_count,
        "note": "既有任务保留其冻结的评分依据与规则版本，新规则仅作用于换版后生成的任务",
    }

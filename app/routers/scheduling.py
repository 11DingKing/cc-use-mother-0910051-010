"""检查任务调度接口。

覆盖：频率规则版本管理、执法人员容量管理、季度任务生成（固定评分依据与
规则版本、容量约束排期）、延期/机构停业/风险突升/结果回写触发的重排，
以及批量重排。所有写操作在单个事务内完成，提交前做一致性校验，
失败整体回滚；已执行任务冻结，不可排期的任务保留为明确缺口。
"""

import json
from datetime import date, timedelta
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    Institution, ComplianceScore, Inspector, RuleVersion,
    InspectionTask, TaskEvent, TaskStatus, TaskKind,
    TaskEventAction, ReplanTrigger, CluePriority, ComplianceGrade,
)
from .. import schemas
from ..scheduling import (
    ACTIVE_TASK_STATUSES, FROZEN_TASK_STATUSES,
    OPERATING_NORMAL, OPERATING_CLOSED,
    generate_tasks, replan_window, create_task_candidates,
    parse_rules, rule_for_grade, get_active_rule_version,
    validate_consistency, _record_event, _decision, _institution_names,
)
from .compliance_score import calculate_compliance_score, save_compliance_score

router = APIRouter()

DEFAULT_WINDOW_DAYS = 92


def _resolve_window(
    window_start: Optional[date], window_end: Optional[date]
) -> tuple:
    start = window_start or date.today()
    end = window_end or (start + timedelta(days=DEFAULT_WINDOW_DAYS))
    if end < start:
        raise HTTPException(status_code=400, detail="排期窗口结束日期不能早于开始日期")
    return start, end


def _run_in_transaction(db: Session, error_prefix: str, fn):
    """在保存点内执行调度操作：一致性校验通过后提交，任何失败只回滚本操作。

    HTTPException（参数/状态校验失败）直接向上抛出，保存点已将其前的
    本操作改动回滚；其他异常回滚整个会话事务并返回 500。
    """
    try:
        with db.begin_nested():
            result = fn()
            validate_consistency(db)
        db.commit()
        return result
    except HTTPException:
        raise
    except Exception as e:
        db.rollback()
        raise HTTPException(
            status_code=500, detail=f"{error_prefix}，事务已回滚：{e}"
        )


def _build_replan_result(
    trigger: ReplanTrigger,
    window_start: date,
    window_end: date,
    decisions: List[dict],
) -> schemas.ReplanResult:
    def count(action):
        return sum(1 for d in decisions if d["action"] == action)

    return schemas.ReplanResult(
        trigger=trigger,
        window_start=window_start,
        window_end=window_end,
        total_decisions=len(decisions),
        kept_count=count(TaskEventAction.KEPT),
        rescheduled_count=count(TaskEventAction.RESCHEDULED),
        cancelled_count=count(TaskEventAction.CANCELLED),
        gap_count=count(TaskEventAction.GAP),
        created_count=count(TaskEventAction.CREATED),
        decisions=[schemas.TaskDecision(**d) for d in decisions],
    )


def _rule_version_out(rv: RuleVersion) -> schemas.RuleVersionOut:
    return schemas.RuleVersionOut(
        id=rv.id,
        version_code=rv.version_code,
        name=rv.name,
        rules=parse_rules(rv),
        is_active=rv.is_active,
        remark=rv.remark,
        created_at=rv.created_at,
    )


def _inspector_out(db: Session, inspector: Inspector) -> schemas.InspectorOut:
    active_count = db.query(InspectionTask).filter(
        InspectionTask.inspector_id == inspector.id,
        InspectionTask.status.in_(ACTIVE_TASK_STATUSES),
    ).count()
    return schemas.InspectorOut(
        id=inspector.id,
        name=inspector.name,
        daily_capacity=inspector.daily_capacity,
        is_active=inspector.is_active,
        active_task_count=active_count,
        created_at=inspector.created_at,
    )


def _task_out(task: InspectionTask) -> schemas.InspectionTaskOut:
    return schemas.InspectionTaskOut(
        id=task.id,
        task_no=task.task_no,
        institution_id=task.institution_id,
        institution_name=task.institution.name if task.institution else None,
        compliance_score_id=task.compliance_score_id,
        rule_version_id=task.rule_version_id,
        version_code=task.rule_version.version_code if task.rule_version else None,
        task_kind=task.task_kind,
        round_no=task.round_no,
        priority=task.priority,
        priority_reason=task.priority_reason,
        due_date=task.due_date,
        scheduled_date=task.scheduled_date,
        inspector_id=task.inspector_id,
        inspector_name=task.inspector.name if task.inspector else None,
        status=task.status,
        unschedulable_reason=task.unschedulable_reason,
        result=task.result,
        actual_date=task.actual_date,
        created_at=task.created_at,
        updated_at=task.updated_at,
    )


# ============ 频率规则版本 ============


@router.post("/rule-versions", response_model=schemas.RuleVersionOut)
def create_rule_version(
    data: schemas.RuleVersionCreate, db: Session = Depends(get_db)
):
    existing = db.query(RuleVersion).filter(
        RuleVersion.version_code == data.version_code
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="规则版本编码已存在")
    if not data.rules:
        raise HTTPException(status_code=400, detail="规则内容不能为空")
    for key in data.rules:
        try:
            ComplianceGrade(key)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"无效的合规等级键「{key}」，仅支持 A/B/C/D"
            )

    try:
        if data.activate:
            db.query(RuleVersion).update({RuleVersion.is_active: False})
        rules_json = json.dumps(
            {k: v.model_dump(mode="json") for k, v in data.rules.items()},
            ensure_ascii=False,
        )
        rule_version = RuleVersion(
            version_code=data.version_code,
            name=data.name,
            rules=rules_json,
            is_active=data.activate,
            remark=data.remark,
        )
        db.add(rule_version)
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"规则版本创建失败，事务已回滚：{e}")

    db.refresh(rule_version)
    return _rule_version_out(rule_version)


@router.get("/rule-versions", response_model=List[schemas.RuleVersionOut])
def list_rule_versions(db: Session = Depends(get_db)):
    versions = db.query(RuleVersion).order_by(RuleVersion.id.desc()).all()
    return [_rule_version_out(rv) for rv in versions]


@router.post("/rule-versions/{version_id}/activate", response_model=schemas.RuleVersionOut)
def activate_rule_version(version_id: int, db: Session = Depends(get_db)):
    rule_version = db.query(RuleVersion).filter(RuleVersion.id == version_id).first()
    if not rule_version:
        raise HTTPException(status_code=404, detail="规则版本不存在")
    try:
        db.query(RuleVersion).update({RuleVersion.is_active: False})
        rule_version.is_active = True
        db.commit()
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"启用规则版本失败，事务已回滚：{e}")
    db.refresh(rule_version)
    return _rule_version_out(rule_version)


# ============ 执法人员 ============


@router.post("/inspectors", response_model=schemas.InspectorOut)
def create_inspector(
    data: schemas.InspectorCreate, db: Session = Depends(get_db)
):
    existing = db.query(Inspector).filter(Inspector.name == data.name).first()
    if existing:
        raise HTTPException(status_code=400, detail="执法人员姓名已存在")
    inspector = Inspector(name=data.name, daily_capacity=data.daily_capacity)
    db.add(inspector)
    db.commit()
    db.refresh(inspector)
    return _inspector_out(db, inspector)


@router.get("/inspectors", response_model=List[schemas.InspectorOut])
def list_inspectors(db: Session = Depends(get_db)):
    inspectors = db.query(Inspector).order_by(Inspector.id).all()
    return [_inspector_out(db, i) for i in inspectors]


@router.put("/inspectors/{inspector_id}", response_model=schemas.InspectorOut)
def update_inspector(
    inspector_id: int,
    data: schemas.InspectorUpdate,
    db: Session = Depends(get_db),
):
    inspector = db.query(Inspector).filter(Inspector.id == inspector_id).first()
    if not inspector:
        raise HTTPException(status_code=404, detail="执法人员不存在")
    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(inspector, key, value)
    db.commit()
    db.refresh(inspector)
    return _inspector_out(db, inspector)


# ============ 任务生成 ============


@router.post("/tasks/generate", response_model=schemas.TaskGenerationResult)
def generate_inspection_tasks(
    data: schemas.TaskGenerateRequest, db: Session = Depends(get_db)
):
    if data.period_end < data.period_start:
        raise HTTPException(status_code=400, detail="排期窗口结束日期不能早于开始日期")

    def _work():
        return generate_tasks(
            db,
            data.period_start,
            data.period_end,
            institution_id=data.institution_id,
            rule_version_id=data.rule_version_id,
        )

    decisions, rule_version = _run_in_transaction(db, "检查任务生成失败", _work)
    return schemas.TaskGenerationResult(
        rule_version_id=rule_version.id,
        version_code=rule_version.version_code,
        period_start=data.period_start,
        period_end=data.period_end,
        created_count=sum(1 for d in decisions if d["action"] == TaskEventAction.CREATED),
        gap_count=sum(1 for d in decisions if d["action"] == TaskEventAction.GAP),
        decisions=[schemas.TaskDecision(**d) for d in decisions],
    )


# ============ 任务查询 ============


@router.get("/tasks", response_model=List[schemas.InspectionTaskOut])
def list_tasks(
    institution_id: Optional[int] = None,
    status: Optional[TaskStatus] = None,
    inspector_id: Optional[int] = None,
    task_kind: Optional[TaskKind] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    db: Session = Depends(get_db),
):
    query = db.query(InspectionTask)
    if institution_id is not None:
        query = query.filter(InspectionTask.institution_id == institution_id)
    if status is not None:
        query = query.filter(InspectionTask.status == status)
    if inspector_id is not None:
        query = query.filter(InspectionTask.inspector_id == inspector_id)
    if task_kind is not None:
        query = query.filter(InspectionTask.task_kind == task_kind)
    if date_from is not None:
        query = query.filter(InspectionTask.scheduled_date >= date_from)
    if date_to is not None:
        query = query.filter(InspectionTask.scheduled_date <= date_to)

    tasks = query.order_by(
        InspectionTask.scheduled_date.is_(None),
        InspectionTask.scheduled_date,
        InspectionTask.id,
    ).all()
    return [_task_out(t) for t in tasks]


@router.get("/tasks/gaps", response_model=List[schemas.InspectionTaskOut])
def list_task_gaps(db: Session = Depends(get_db)):
    """待排期缺口：因容量不足等原因暂无法安排的任务，明确保留而非丢弃。"""
    tasks = db.query(InspectionTask).filter(
        InspectionTask.status == TaskStatus.PENDING_ASSIGNMENT
    ).order_by(InspectionTask.due_date, InspectionTask.id).all()
    return [_task_out(t) for t in tasks]


@router.get("/tasks/{task_id}", response_model=schemas.InspectionTaskDetail)
def get_task(task_id: int, db: Session = Depends(get_db)):
    task = db.query(InspectionTask).filter(InspectionTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="检查任务不存在")
    events = db.query(TaskEvent).filter(
        TaskEvent.task_id == task_id
    ).order_by(TaskEvent.id).all()
    return schemas.InspectionTaskDetail(
        **_task_out(task).model_dump(),
        events=[schemas.TaskEventOut.model_validate(e) for e in events],
    )


# ============ 触发器：延期 ============


@router.post("/tasks/{task_id}/delay", response_model=schemas.ReplanResult)
def delay_task(
    task_id: int,
    data: schemas.TaskDelayRequest,
    db: Session = Depends(get_db),
):
    task = db.query(InspectionTask).filter(InspectionTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="检查任务不存在")
    if task.status in FROZEN_TASK_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"任务状态为「{task.status.value}」，已执行或已取消的任务不能延期"
        )

    window_start, window_end = _resolve_window(data.window_start, data.window_end)
    pinned, not_before = {}, {}
    if data.new_date is not None:
        pinned[task.id] = data.new_date
    else:
        base = task.scheduled_date or window_start
        not_before[task.id] = base + timedelta(days=1)

    reason = data.reason or (
        f"检查任务{task.task_no}延期"
        + (f"至{data.new_date}" if data.new_date else "，由系统重新安排档期")
    )

    def _work():
        return replan_window(
            db, window_start, window_end,
            trigger=ReplanTrigger.DELAY,
            reason=reason,
            pinned=pinned,
            not_before=not_before,
        )

    decisions = _run_in_transaction(db, "任务延期失败", _work)
    return _build_replan_result(
        ReplanTrigger.DELAY, window_start, window_end, decisions
    )


# ============ 触发器：检查结果回写 ============


@router.post("/tasks/{task_id}/result", response_model=schemas.ReplanResult)
def writeback_task_result(
    task_id: int,
    data: schemas.ResultWritebackRequest,
    db: Session = Depends(get_db),
):
    task = db.query(InspectionTask).filter(InspectionTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="检查任务不存在")
    if task.status in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
        raise HTTPException(
            status_code=409,
            detail=f"任务状态为「{task.status.value}」，不能重复回写检查结果"
        )
    if task.status == TaskStatus.PENDING_ASSIGNMENT:
        raise HTTPException(status_code=409, detail="任务尚未排期，无法回写检查结果")

    window_start, window_end = _resolve_window(data.window_start, data.window_end)

    def _work():
        names = _institution_names(db)
        decisions: List[dict] = []

        task.status = TaskStatus.COMPLETED
        task.result = data.result
        task.actual_date = data.actual_date or date.today()
        verdict = "合格" if data.passed else "不合格"
        complete_reason = f"检查结果回写：{verdict}。{data.result}"
        decisions.append(_decision(
            names, task.institution_id, TaskEventAction.COMPLETED,
            complete_reason, task=task,
            old_scheduled_date=task.scheduled_date,
            new_scheduled_date=task.scheduled_date,
            old_inspector_id=task.inspector_id,
            new_inspector_id=task.inspector_id,
        ))
        _record_event(
            db, task, ReplanTrigger.RESULT_WRITEBACK,
            TaskEventAction.COMPLETED, complete_reason,
            old_scheduled_date=task.scheduled_date,
            new_scheduled_date=task.scheduled_date,
            old_inspector_id=task.inspector_id,
            new_inspector_id=task.inspector_id,
        )

        new_task_ids = set()
        if not data.passed:
            # 检查不合格：按当前启用规则版本（缺省沿用任务固定版本）生成复查任务
            rule_version = get_active_rule_version(db) or task.rule_version
            follow_up = InspectionTask(
                institution_id=task.institution_id,
                compliance_score_id=task.compliance_score_id,
                rule_version_id=rule_version.id,
                task_kind=TaskKind.FOLLOW_UP,
                round_no=task.round_no,
                priority=CluePriority.HIGH,
                priority_reason=(
                    f"任务{task.task_no}检查结果不合格，"
                    f"按规则版本{rule_version.version_code}生成高优先级复查任务"
                ),
                due_date=date.today() + timedelta(days=30),
                status=TaskStatus.PENDING_ASSIGNMENT,
            )
            db.add(follow_up)
            db.flush()
            follow_up.task_no = f"JC{follow_up.id:06d}"
            new_task_ids.add(follow_up.id)
        db.flush()

        decisions += replan_window(
            db, window_start, window_end,
            trigger=ReplanTrigger.RESULT_WRITEBACK,
            reason=f"任务{task.task_no}结果回写（{verdict}）",
            new_task_ids=new_task_ids,
        )
        return decisions

    decisions = _run_in_transaction(db, "检查结果回写失败", _work)
    return _build_replan_result(
        ReplanTrigger.RESULT_WRITEBACK, window_start, window_end, decisions
    )


# ============ 触发器：机构停业/恢复 ============


@router.post("/institutions/{institution_id}/closure", response_model=schemas.ReplanResult)
def institution_closure(
    institution_id: int,
    data: schemas.InstitutionClosureRequest,
    db: Session = Depends(get_db),
):
    institution = db.query(Institution).filter(Institution.id == institution_id).first()
    if not institution:
        raise HTTPException(status_code=404, detail="机构不存在")

    window_start, window_end = _resolve_window(data.window_start, data.window_end)

    def _work():
        names = _institution_names(db)
        decisions: List[dict] = []

        institution.operating_status = (
            OPERATING_CLOSED if data.closed else OPERATING_NORMAL
        )
        if data.closed:
            active_tasks = db.query(InspectionTask).filter(
                InspectionTask.institution_id == institution_id,
                InspectionTask.status.in_(ACTIVE_TASK_STATUSES),
            ).all()
            for task in active_tasks:
                old_date, old_inspector = task.scheduled_date, task.inspector_id
                task.status = TaskStatus.CANCELLED
                task.unschedulable_reason = None
                cancel_reason = (
                    f"机构停业，检查任务取消"
                    + (f"：{data.reason}" if data.reason else "")
                )
                decisions.append(_decision(
                    names, institution_id, TaskEventAction.CANCELLED,
                    cancel_reason, task=task,
                    old_scheduled_date=old_date, old_inspector_id=old_inspector,
                ))
                _record_event(
                    db, task, ReplanTrigger.CLOSURE, TaskEventAction.CANCELLED,
                    cancel_reason,
                    old_scheduled_date=old_date, old_inspector_id=old_inspector,
                )
            db.flush()

        # 停业释放的容量可用于补入其他机构的待排期缺口
        decisions += replan_window(
            db, window_start, window_end,
            trigger=ReplanTrigger.CLOSURE,
            reason=data.reason or ("机构停业" if data.closed else "机构恢复营业"),
        )
        return decisions

    decisions = _run_in_transaction(db, "机构停业处理失败", _work)
    return _build_replan_result(
        ReplanTrigger.CLOSURE, window_start, window_end, decisions
    )


# ============ 触发器：风险突升 ============


@router.post("/institutions/{institution_id}/risk-surge", response_model=schemas.ReplanResult)
def institution_risk_surge(
    institution_id: int,
    data: schemas.RiskSurgeRequest,
    db: Session = Depends(get_db),
):
    institution = db.query(Institution).filter(Institution.id == institution_id).first()
    if not institution:
        raise HTTPException(status_code=404, detail="机构不存在")

    window_start, window_end = _resolve_window(data.window_start, data.window_end)

    def _work():
        names = _institution_names(db)
        decisions: List[dict] = []

        if data.new_score_id is not None:
            new_score = db.query(ComplianceScore).filter(
                ComplianceScore.id == data.new_score_id
            ).first()
            if not new_score:
                raise HTTPException(status_code=404, detail="评分记录不存在")
            if new_score.institution_id != institution_id:
                raise HTTPException(status_code=400, detail="评分记录不属于该机构")
        else:
            result = calculate_compliance_score(institution_id, db)
            new_score = save_compliance_score(
                result, db, remark="风险突升重评", commit=False
            )

        active_rule_version = get_active_rule_version(db)
        active_tasks = db.query(InspectionTask).filter(
            InspectionTask.institution_id == institution_id,
            InspectionTask.status.in_(ACTIVE_TASK_STATUSES),
        ).all()

        # 已执行（进行中/已完成）与已取消的任务冻结，不在此列；
        # 仅活动任务按新评分调整优先级与应检日期，并说明原因。
        for task in active_tasks:
            rule_version = active_rule_version or task.rule_version
            if rule_version is None:
                continue
            rules = parse_rules(rule_version)
            rule = rule_for_grade(rules, new_score.grade)
            new_priority = CluePriority(rule["priority"])
            new_due = new_score.scored_at.date() + timedelta(
                days=rule["frequency_months"] * 30
            )

            old_priority = task.priority
            old_due = task.due_date
            old_score_id = task.compliance_score_id
            changes = []
            if new_priority != old_priority:
                changes.append(f"优先级由「{old_priority.value}」调整为「{new_priority.value}」")
            # 风险变化只提前不延后应检日期，避免高风险机构被推到后面
            if old_due is None or new_due < old_due:
                if new_due != old_due:
                    changes.append(f"应检日期由{old_due}提前至{new_due}")
                task.due_date = new_due
            if not changes:
                continue

            task.priority = new_priority
            task.compliance_score_id = new_score.id
            task.priority_reason = (
                f"风险重评：机构等级{new_score.grade.value}"
                f"（{new_score.total_score}分，评分#{new_score.id}），"
                f"按规则版本{rule_version.version_code}，" + "，".join(changes)
            )
            decisions.append(_decision(
                names, institution_id, TaskEventAction.PRIORITY_CHANGED,
                task.priority_reason, task=task,
                old_priority=old_priority, new_priority=new_priority,
            ))
            _record_event(
                db, task, ReplanTrigger.RISK_SURGE,
                TaskEventAction.PRIORITY_CHANGED, task.priority_reason,
                old_priority=old_priority, new_priority=new_priority,
                old_score_id=old_score_id, new_score_id=new_score.id,
            )
        db.flush()

        # 本季度已无活动的常规任务（例如均已执行完毕）时，按新评分补生成
        has_active_regular = any(
            t.task_kind == TaskKind.REGULAR for t in active_tasks
        )
        new_task_ids = set()
        if (
            not has_active_regular
            and active_rule_version is not None
            and (institution.operating_status or OPERATING_NORMAL) == OPERATING_NORMAL
        ):
            ids, pre_decisions = create_task_candidates(
                db, window_start, window_end, active_rule_version,
                institution_id=institution_id,
            )
            new_task_ids = set(ids)
            decisions += pre_decisions

        decisions += replan_window(
            db, window_start, window_end,
            trigger=ReplanTrigger.RISK_SURGE,
            reason=data.reason or f"机构风险重评为{new_score.grade.value}级",
            new_task_ids=new_task_ids,
        )
        return decisions

    decisions = _run_in_transaction(db, "风险突升处理失败", _work)
    return _build_replan_result(
        ReplanTrigger.RISK_SURGE, window_start, window_end, decisions
    )


# ============ 批量重排 ============


@router.post("/replan", response_model=schemas.ReplanResult)
def batch_replan(data: schemas.ReplanRequest, db: Session = Depends(get_db)):
    if data.window_end < data.window_start:
        raise HTTPException(status_code=400, detail="排期窗口结束日期不能早于开始日期")

    def _work():
        return replan_window(
            db, data.window_start, data.window_end,
            trigger=ReplanTrigger.MANUAL_REPLAN,
            reason=data.reason or "批量重排",
        )

    decisions = _run_in_transaction(db, "批量重排失败", _work)
    return _build_replan_result(
        ReplanTrigger.MANUAL_REPLAN, data.window_start, data.window_end, decisions
    )


# ============ 调整留痕 ============


@router.get("/events", response_model=List[schemas.TaskEventOut])
def list_task_events(
    task_id: Optional[int] = None,
    trigger: Optional[ReplanTrigger] = None,
    limit: int = Query(200, ge=1, le=1000),
    db: Session = Depends(get_db),
):
    query = db.query(TaskEvent)
    if task_id is not None:
        query = query.filter(TaskEvent.task_id == task_id)
    if trigger is not None:
        query = query.filter(TaskEvent.trigger == trigger)
    events = query.order_by(TaskEvent.id.desc()).limit(limit).all()
    return [schemas.TaskEventOut.model_validate(e) for e in events]

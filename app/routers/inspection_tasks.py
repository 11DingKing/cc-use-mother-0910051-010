"""
有容量约束检查任务的 API。

- 执法人员与请假容量管理
- 季度计划生成（固定风险评分与频率规则版本）
- 延期、机构停业/复业、风险突升、检查结果回写等事件处理
- 批量重排（明确保留无法安排的缺口）
- 任务变更审计轨迹（含优先级变化原因）
- 规则版本查询与换版
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import date
from typing import List, Optional

from ..database import get_db
from ..models import (
    Inspector, InspectorBlock, InspectionTask, TaskStatus, TaskChangeLog,
    ScheduleGap, CluePriority,
)
from .. import schemas
from .. import task_service
from sqlalchemy import case

# 业务优先级排序（高>中>低），枚举名称的字母序不可直接用于排序
_PRIORITY_ORDER = case(
    (InspectionTask.priority == CluePriority.HIGH, 0),
    (InspectionTask.priority == CluePriority.MEDIUM, 1),
    (InspectionTask.priority == CluePriority.LOW, 2),
    else_=3,
)

router = APIRouter()


# ---------------------------------------------------------------------------
# 执法人员 / 容量
# ---------------------------------------------------------------------------

@router.post("/inspectors", response_model=schemas.InspectorSchema, tags=["执法人员容量"])
def create_inspector(payload: schemas.InspectorCreate, db: Session = Depends(get_db)):
    if db.query(Inspector).filter(Inspector.name == payload.name).first():
        raise HTTPException(status_code=400, detail=f"执法人员 {payload.name} 已存在")
    inspector = Inspector(**payload.model_dump())
    db.add(inspector)
    db.commit()
    db.refresh(inspector)
    return inspector


@router.get("/inspectors", response_model=List[schemas.InspectorSchema], tags=["执法人员容量"])
def list_inspectors(include_inactive: bool = False, db: Session = Depends(get_db)):
    query = db.query(Inspector)
    if not include_inactive:
        query = query.filter(Inspector.is_active == True)  # noqa: E712
    return query.order_by(Inspector.id.asc()).all()


@router.patch("/inspectors/{inspector_id}", response_model=schemas.InspectorSchema, tags=["执法人员容量"])
def update_inspector(inspector_id: int, payload: schemas.InspectorUpdate, db: Session = Depends(get_db)):
    inspector = db.query(Inspector).get(inspector_id)
    if inspector is None:
        raise HTTPException(status_code=404, detail="执法人员不存在")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(inspector, key, value)
    db.commit()
    db.refresh(inspector)
    return inspector


@router.post("/inspectors/{inspector_id}/blocks", response_model=schemas.InspectorBlockSchema, tags=["执法人员容量"])
def add_inspector_block(inspector_id: int, payload: schemas.InspectorBlockCreate, db: Session = Depends(get_db)):
    inspector = db.query(Inspector).get(inspector_id)
    if inspector is None:
        raise HTTPException(status_code=404, detail="执法人员不存在")
    exists = db.query(InspectorBlock).filter(
        InspectorBlock.inspector_id == inspector_id,
        InspectorBlock.block_date == payload.block_date
    ).first()
    if exists:
        raise HTTPException(status_code=400, detail="该日期已存在不可用记录")
    block = InspectorBlock(inspector_id=inspector_id, **payload.model_dump())
    db.add(block)
    db.commit()
    db.refresh(block)
    return block


@router.get("/inspectors/{inspector_id}/blocks", response_model=List[schemas.InspectorBlockSchema], tags=["执法人员容量"])
def list_inspector_blocks(inspector_id: int, db: Session = Depends(get_db)):
    if db.query(Inspector).get(inspector_id) is None:
        raise HTTPException(status_code=404, detail="执法人员不存在")
    return db.query(InspectorBlock).filter(
        InspectorBlock.inspector_id == inspector_id
    ).order_by(InspectorBlock.block_date.asc()).all()


# ---------------------------------------------------------------------------
# 规则版本
# ---------------------------------------------------------------------------

@router.get("/rule-versions", response_model=List[schemas.RuleVersionSchema], tags=["评分规则版本"])
def list_rule_versions(db: Session = Depends(get_db)):
    from ..models import RuleVersion
    return db.query(RuleVersion).order_by(RuleVersion.id.desc()).all()


@router.get("/rule-versions/active", response_model=schemas.RuleVersionSchema, tags=["评分规则版本"])
def get_active_rule_version(db: Session = Depends(get_db)):
    from ..rule_engine import get_active_rule_version
    return get_active_rule_version(db)


@router.post("/rule-versions", response_model=schemas.RuleUpgradeResult, tags=["评分规则版本"])
def upgrade_rule_version(payload: schemas.RuleVersionCreate, db: Session = Depends(get_db)):
    try:
        return task_service.upgrade_rule_version(
            db,
            version_code=payload.version_code,
            effective_from=payload.effective_from,
            change_summary=payload.change_summary,
            rules=payload.rules,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# 季度计划生成
# ---------------------------------------------------------------------------

@router.post("/quarterly-plan", response_model=schemas.QuarterlyPlanResult, tags=["检查任务"])
def generate_quarterly_plan(
    payload: schemas.QuarterlyPlanGenerateRequest,
    db: Session = Depends(get_db)
):
    result = task_service.generate_quarterly_plan(
        db,
        start_date=payload.start_date,
        institution_ids=payload.institution_ids,
        rule_version_id=payload.rule_version_id,
    )
    return result


# ---------------------------------------------------------------------------
# 任务查询
# ---------------------------------------------------------------------------

@router.get("/tasks", response_model=List[schemas.InspectionTaskDetail], tags=["检查任务"])
def list_tasks(
    institution_id: Optional[int] = None,
    status: Optional[TaskStatus] = None,
    inspector_id: Optional[int] = None,
    quarter: Optional[str] = None,
    scheduled_from: Optional[date] = None,
    scheduled_to: Optional[date] = None,
    db: Session = Depends(get_db)
):
    query = db.query(InspectionTask)
    if institution_id is not None:
        query = query.filter(InspectionTask.institution_id == institution_id)
    if status is not None:
        query = query.filter(InspectionTask.status == status)
    if inspector_id is not None:
        query = query.filter(InspectionTask.inspector_id == inspector_id)
    if quarter is not None:
        query = query.filter(InspectionTask.quarter == quarter)
    if scheduled_from is not None:
        query = query.filter(InspectionTask.scheduled_date >= scheduled_from)
    if scheduled_to is not None:
        query = query.filter(InspectionTask.scheduled_date <= scheduled_to)
    return query.order_by(
        _PRIORITY_ORDER,
        InspectionTask.frozen_score.asc(),
        InspectionTask.earliest_date.asc()
    ).all()


@router.get("/tasks/{task_id}", response_model=schemas.InspectionTaskDetail, tags=["检查任务"])
def get_task(task_id: int, db: Session = Depends(get_db)):
    task = db.query(InspectionTask).get(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return task


@router.get("/tasks/{task_id}/logs", response_model=List[schemas.TaskChangeLogSchema], tags=["检查任务"])
def list_task_logs(task_id: int, db: Session = Depends(get_db)):
    if db.query(InspectionTask).get(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return db.query(TaskChangeLog).filter(
        TaskChangeLog.task_id == task_id
    ).order_by(TaskChangeLog.id.asc()).all()


@router.get("/gaps", response_model=List[schemas.ScheduleGapDetail], tags=["排期缺口"])
def list_gaps(
    unresolved_only: bool = True,
    institution_id: Optional[int] = None,
    db: Session = Depends(get_db)
):
    query = db.query(ScheduleGap)
    if unresolved_only:
        query = query.filter(ScheduleGap.resolved == False)  # noqa: E712
    if institution_id is not None:
        query = query.filter(ScheduleGap.institution_id == institution_id)
    return query.order_by(ScheduleGap.id.asc()).all()


# ---------------------------------------------------------------------------
# 事件：延期 / 结果回写 / 风险突升 / 停业复业
# ---------------------------------------------------------------------------

@router.post("/tasks/{task_id}/postpone", response_model=schemas.InspectionTaskDetail, tags=["检查任务事件"])
def postpone_task(task_id: int, payload: schemas.TaskPostponeRequest, db: Session = Depends(get_db)):
    try:
        return task_service.postpone_task(
            db, task_id, new_date=payload.new_date, reason=payload.reason
        )
    except task_service.TaskLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/tasks/{task_id}/start", response_model=schemas.InspectionTaskDetail, tags=["检查任务事件"])
def start_task(task_id: int, db: Session = Depends(get_db)):
    try:
        return task_service.mark_task_in_progress(db, task_id)
    except task_service.TaskLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/tasks/{task_id}/result", response_model=schemas.InspectionTaskDetail, tags=["检查任务事件"])
def record_task_result(task_id: int, payload: schemas.TaskResultRequest, db: Session = Depends(get_db)):
    try:
        return task_service.record_task_result(
            db, task_id, result=payload.result, inspection_date=payload.inspection_date
        )
    except task_service.TaskLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/institutions/{institution_id}/risk-surge", tags=["检查任务事件"])
def risk_surge(institution_id: int, payload: schemas.RiskSurgeRequest, db: Session = Depends(get_db)):
    try:
        return task_service.handle_risk_surge(db, institution_id, reason=payload.reason)
    except task_service.SchedulingError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/institutions/{institution_id}/suspend", tags=["检查任务事件"])
def suspend_institution(institution_id: int, payload: schemas.InstitutionSuspendRequest, db: Session = Depends(get_db)):
    try:
        return task_service.suspend_institution(
            db, institution_id,
            reason=payload.reason,
            expected_resume_date=payload.expected_resume_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/institutions/{institution_id}/resume", tags=["检查任务事件"])
def resume_institution(institution_id: int, db: Session = Depends(get_db)):
    try:
        return task_service.resume_institution(db, institution_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


# ---------------------------------------------------------------------------
# 批量重排
# ---------------------------------------------------------------------------

@router.post("/batch-reschedule", response_model=schemas.BatchRescheduleResult, tags=["检查任务事件"])
def batch_reschedule(payload: schemas.BatchRescheduleRequest, db: Session = Depends(get_db)):
    result = task_service.batch_reschedule(
        db,
        from_date=payload.from_date,
        institution_ids=payload.institution_ids,
        reason=payload.reason,
    )
    return {
        "batch_id": result["batch_id"],
        "reason": result["reason"],
        "kept_count": len(result["kept"]),
        "rescheduled_count": len(result["rescheduled"]),
        "gaps": result["gaps"],
        "locked_tasks_untouched": result["locked_tasks_untouched"],
        "kept_task_ids": [t.id for t in result["kept"]],
        "rescheduled_task_ids": [t.id for t in result["rescheduled"]],
    }

from pydantic import BaseModel, Field
from datetime import date, datetime
from typing import Optional, List
from .models import (
    InstitutionType, ProcedureCategory, SurgeryLevel,
    QualificationType, ClueType, ClueStatus, CluePriority,
    ComplianceGrade, ScoreItem, InspectionFrequency, PlanStatus,
    TaskStatus, TaskChangeType, ChangeTrigger, GapReason, RuleStatus
)


class InstitutionBase(BaseModel):
    name: str
    unified_social_code: str
    institution_type: InstitutionType
    legal_person: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    registration_date: Optional[date] = None
    business_scope: Optional[str] = None


class InstitutionCreate(InstitutionBase):
    pass


class InstitutionUpdate(BaseModel):
    name: Optional[str] = None
    unified_social_code: Optional[str] = None
    institution_type: Optional[InstitutionType] = None
    legal_person: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    registration_date: Optional[date] = None
    business_scope: Optional[str] = None


class Institution(InstitutionBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class InstitutionLicenseBase(BaseModel):
    license_number: str
    issuing_authority: Optional[str] = None
    issue_date: Optional[date] = None
    valid_until: Optional[date] = None
    approved_surgeries: Optional[str] = None
    is_valid: bool = True


class InstitutionLicenseCreate(InstitutionLicenseBase):
    institution_id: int


class InstitutionLicense(InstitutionLicenseBase):
    id: int
    created_at: datetime
    institution: Optional[Institution] = None

    class Config:
        from_attributes = True


class PractitionerBase(BaseModel):
    name: str
    id_card: str
    gender: Optional[str] = None
    birth_date: Optional[date] = None
    institution_id: Optional[int] = None
    position: Optional[str] = None


class PractitionerCreate(PractitionerBase):
    pass


class PractitionerUpdate(BaseModel):
    name: Optional[str] = None
    id_card: Optional[str] = None
    gender: Optional[str] = None
    birth_date: Optional[date] = None
    institution_id: Optional[int] = None
    position: Optional[str] = None


class Practitioner(PractitionerBase):
    id: int
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class PractitionerQualificationBase(BaseModel):
    qualification_type: QualificationType
    certificate_number: str
    issuing_authority: Optional[str] = None
    issue_date: Optional[date] = None
    valid_until: Optional[date] = None
    practice_scope: Optional[str] = None
    is_valid: bool = True


class PractitionerQualificationCreate(PractitionerQualificationBase):
    practitioner_id: int


class PractitionerQualification(PractitionerQualificationBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class PractitionerWithQualifications(Practitioner):
    qualifications: List[PractitionerQualification] = []


class ProcedureBase(BaseModel):
    name: str
    code: str
    category: ProcedureCategory
    surgery_level: Optional[SurgeryLevel] = None
    description: Optional[str] = None
    requires_qualification: Optional[str] = None


class ProcedureCreate(ProcedureBase):
    pass


class ProcedureUpdate(BaseModel):
    name: Optional[str] = None
    code: Optional[str] = None
    category: Optional[ProcedureCategory] = None
    surgery_level: Optional[SurgeryLevel] = None
    description: Optional[str] = None
    requires_qualification: Optional[str] = None


class Procedure(ProcedureBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class InstitutionAuthorizedProcedureBase(BaseModel):
    institution_id: int
    procedure_id: int
    authorized_date: Optional[date] = None
    remark: Optional[str] = None


class InstitutionAuthorizedProcedureCreate(InstitutionAuthorizedProcedureBase):
    pass


class InstitutionAuthorizedProcedure(InstitutionAuthorizedProcedureBase):
    id: int
    created_at: datetime
    procedure: Optional[Procedure] = None

    class Config:
        from_attributes = True


class PractitionerAuthorizedProcedureBase(BaseModel):
    practitioner_id: int
    procedure_id: int
    authorized_date: Optional[date] = None
    remark: Optional[str] = None


class PractitionerAuthorizedProcedureCreate(PractitionerAuthorizedProcedureBase):
    pass


class PractitionerAuthorizedProcedure(PractitionerAuthorizedProcedureBase):
    id: int
    created_at: datetime
    procedure: Optional[Procedure] = None

    class Config:
        from_attributes = True


class ActualProcedureRecordBase(BaseModel):
    institution_id: int
    practitioner_id: int
    procedure_id: int
    procedure_date: date
    patient_count: int = 1
    remark: Optional[str] = None


class ActualProcedureRecordCreate(ActualProcedureRecordBase):
    pass


class ActualProcedureRecord(ActualProcedureRecordBase):
    id: int
    is_over_range: bool = False
    over_range_detail: Optional[str] = None
    created_at: datetime
    procedure: Optional[Procedure] = None
    practitioner: Optional[Practitioner] = None

    class Config:
        from_attributes = True


class ViolationClueBase(BaseModel):
    clue_type: ClueType
    title: str
    description: str
    institution_id: Optional[int] = None
    practitioner_id: Optional[int] = None
    procedure_id: Optional[int] = None
    source: Optional[str] = None
    priority: CluePriority = CluePriority.MEDIUM


class ViolationClueCreate(ViolationClueBase):
    pass


class ViolationClueUpdate(BaseModel):
    clue_type: Optional[ClueType] = None
    title: Optional[str] = None
    description: Optional[str] = None
    institution_id: Optional[int] = None
    practitioner_id: Optional[int] = None
    procedure_id: Optional[int] = None
    source: Optional[str] = None
    priority: Optional[CluePriority] = None
    status: Optional[ClueStatus] = None
    assignee: Optional[str] = None
    conclusion: Optional[str] = None


class ViolationClue(ViolationClueBase):
    id: int
    status: ClueStatus = ClueStatus.PENDING
    assignee: Optional[str] = None
    assigned_at: Optional[datetime] = None
    conclusion: Optional[str] = None
    verified_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ClueAssign(BaseModel):
    assignee: str


class ClueConclusion(BaseModel):
    status: ClueStatus
    conclusion: str


class InspectionRecordBase(BaseModel):
    clue_id: int
    inspector: str
    inspection_date: date
    content: str
    finding: Optional[str] = None


class InspectionRecordCreate(InspectionRecordBase):
    pass


class InspectionRecord(InspectionRecordBase):
    id: int
    created_at: datetime

    class Config:
        from_attributes = True


class InstitutionComplianceCheck(BaseModel):
    institution_id: int
    institution_name: str
    has_valid_license: bool
    license_detail: Optional[str] = None
    authorized_procedure_count: int
    actual_procedure_count: int
    over_range_count: int
    unlicensed_practitioners: int
    total_practitioners: int
    clues_count: int
    verified_violations: int


class PractitionerComplianceCheck(BaseModel):
    practitioner_id: int
    name: str
    has_valid_doctor_license: bool
    has_valid_practice_license: bool
    has_cosmetology_license: bool
    authorized_procedures: List[str] = []
    actual_procedures: List[str] = []
    over_range_procedures: List[str] = []
    is_unlicensed: bool


class StatsByInstitution(BaseModel):
    institution_id: int
    institution_name: str
    total_clues: int
    verified_violations: int
    pending_clues: int
    over_range_count: int
    unlicensed_ratio: float
    risk_score: float


class StatsByCategory(BaseModel):
    category: ProcedureCategory
    total_actual: int
    over_range_count: int
    over_range_ratio: float
    clue_count: int


class ProblemInstitution(BaseModel):
    institution_id: int
    institution_name: str
    verified_violations: int
    over_range_count: int
    unlicensed_ratio: float
    risk_level: str


class ScoreDeduction(BaseModel):
    item: ScoreItem
    max_score: float
    actual_score: float
    deduction: float
    reason: str


class ComplianceScoreBase(BaseModel):
    institution_id: int
    remark: Optional[str] = None


class ComplianceScoreCreate(ComplianceScoreBase):
    pass


class ComplianceScoreUpdate(BaseModel):
    remark: Optional[str] = None


class ComplianceScore(ComplianceScoreBase):
    id: int
    total_score: float
    grade: ComplianceGrade
    license_valid_score: float
    license_complete_score: float
    no_over_range_score: float
    all_staff_licensed_score: float
    no_quick_training_score: float
    no_false_advertisement_score: float
    no_verified_violation_score: float
    deduction_details: Optional[str] = None
    inspection_frequency: InspectionFrequency
    rule_version_id: Optional[int] = None
    scored_at: datetime
    scoring_period: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class ComplianceScoreDetail(ComplianceScore):
    institution: Optional[Institution] = None
    deduction_list: Optional[List[ScoreDeduction]] = None
    rule_version: Optional["RuleVersionSchema"] = None


class SupervisionPlanBase(BaseModel):
    plan_title: str
    plan_content: str
    planned_date: date
    inspector: Optional[str] = None
    focus_areas: Optional[str] = None


class SupervisionPlanCreate(SupervisionPlanBase):
    compliance_score_id: int
    institution_id: int
    priority: Optional[CluePriority] = CluePriority.MEDIUM


class SupervisionPlanUpdate(BaseModel):
    plan_title: Optional[str] = None
    plan_content: Optional[str] = None
    planned_date: Optional[date] = None
    inspector: Optional[str] = None
    status: Optional[PlanStatus] = None
    priority: Optional[CluePriority] = None
    focus_areas: Optional[str] = None
    actual_inspection_date: Optional[date] = None
    result: Optional[str] = None


class SupervisionPlan(SupervisionPlanBase):
    id: int
    compliance_score_id: int
    institution_id: int
    status: PlanStatus
    priority: CluePriority
    actual_inspection_date: Optional[date] = None
    result: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SupervisionPlanDetail(SupervisionPlan):
    institution: Optional[Institution] = None
    compliance_score: Optional[ComplianceScore] = None


class ScoreCalculationResult(BaseModel):
    institution_id: int
    institution_name: str
    total_score: float
    grade: ComplianceGrade
    inspection_frequency: InspectionFrequency
    deductions: List[ScoreDeduction]
    rule_version_id: Optional[int] = None
    rule_version_code: Optional[str] = None


class BatchScoreResult(BaseModel):
    total_institutions: int
    scored_count: int
    skipped_count: int
    results: List[ScoreCalculationResult]


class PlanGenerationResult(BaseModel):
    institution_id: int
    institution_name: str
    grade: ComplianceGrade
    plans_created: int
    plans: List[SupervisionPlan]


class StatsByComplianceGrade(BaseModel):
    grade: ComplianceGrade
    institution_count: int
    avg_score: float
    total_verified_violations: int
    total_over_range_count: int
    avg_unlicensed_ratio: float


class StatsByInstitutionWithGrade(StatsByInstitution):
    compliance_grade: Optional[ComplianceGrade] = None
    compliance_score: Optional[float] = None
    inspection_frequency: Optional[InspectionFrequency] = None


class ComplianceGradeDistribution(BaseModel):
    grade: ComplianceGrade
    grade_name: str
    count: int
    percentage: float
    score_range: str


# ---------------------------------------------------------------------------
# 规则版本
# ---------------------------------------------------------------------------

class RuleVersionSchema(BaseModel):
    id: int
    version_code: str
    status: RuleStatus
    effective_from: date
    rules_snapshot: str
    change_summary: Optional[str] = None
    created_at: datetime
    deprecated_at: Optional[datetime] = None

    class Config:
        from_attributes = True


class RuleVersionCreate(BaseModel):
    version_code: str = Field(..., description="新版本编码，如 2024Q2-v2")
    effective_from: date
    change_summary: str = ""
    # 需要覆盖的规则片段，例如 {"grade_frequency": {"C": "每季度一次"}}
    rules: Optional[dict] = None


# ---------------------------------------------------------------------------
# 执法人员与容量
# ---------------------------------------------------------------------------

class InspectorCreate(BaseModel):
    name: str
    daily_capacity: int = Field(1, ge=1, le=10, description="每日可承担检查任务数")


class InspectorUpdate(BaseModel):
    name: Optional[str] = None
    daily_capacity: Optional[int] = Field(None, ge=1, le=10)
    is_active: Optional[bool] = None


class InspectorSchema(BaseModel):
    id: int
    name: str
    daily_capacity: int
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class InspectorBlockCreate(BaseModel):
    block_date: date
    reason: Optional[str] = None


class InspectorBlockSchema(BaseModel):
    id: int
    inspector_id: int
    block_date: date
    reason: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


# ---------------------------------------------------------------------------
# 检查任务
# ---------------------------------------------------------------------------

class InspectionTaskBase(BaseModel):
    title: str
    plan_content: Optional[str] = None
    focus_areas: Optional[str] = None


class InspectionTaskSchema(BaseModel):
    id: int
    institution_id: int
    compliance_score_id: Optional[int] = None
    rule_version_id: int
    round_no: int
    quarter: str
    frozen_score: float
    frozen_grade: ComplianceGrade
    frozen_frequency: InspectionFrequency
    priority: CluePriority
    title: str
    plan_content: Optional[str] = None
    focus_areas: Optional[str] = None
    earliest_date: date
    due_date: date
    scheduled_date: Optional[date] = None
    inspector_id: Optional[int] = None
    status: TaskStatus
    actual_inspection_date: Optional[date] = None
    result: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class InspectionTaskDetail(InspectionTaskSchema):
    institution: Optional[Institution] = None
    inspector: Optional[InspectorSchema] = None
    rule_version: Optional[RuleVersionSchema] = None


class TaskChangeLogSchema(BaseModel):
    id: int
    task_id: int
    change_type: TaskChangeType
    trigger: ChangeTrigger
    reason: str
    old_status: Optional[TaskStatus] = None
    new_status: Optional[TaskStatus] = None
    old_priority: Optional[CluePriority] = None
    new_priority: Optional[CluePriority] = None
    old_scheduled_date: Optional[date] = None
    new_scheduled_date: Optional[date] = None
    batch_id: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ScheduleGapSchema(BaseModel):
    id: int
    task_id: int
    institution_id: int
    reason: GapReason
    detail: str
    window_start: date
    window_end: date
    required_capacity: int
    resolved: bool
    resolved_at: Optional[datetime] = None
    batch_id: Optional[str] = None
    created_at: datetime

    class Config:
        from_attributes = True


class ScheduleGapDetail(ScheduleGapSchema):
    task: Optional[InspectionTaskSchema] = None


class QuarterlyPlanGenerateRequest(BaseModel):
    start_date: Optional[date] = None
    institution_ids: Optional[List[int]] = None
    rule_version_id: Optional[int] = None


class QuarterlyPlanResult(BaseModel):
    batch_id: str
    rule_version_code: str
    quarter: str
    tasks_created: int
    tasks_scheduled: int
    gaps: List[ScheduleGapSchema]
    skipped_duplicates: List[dict]
    skipped_suspended: List[dict] = []
    tasks: List[InspectionTaskSchema]


class TaskPostponeRequest(BaseModel):
    new_date: Optional[date] = None
    reason: str = ""


class TaskResultRequest(BaseModel):
    result: str
    inspection_date: Optional[date] = None


class RiskSurgeRequest(BaseModel):
    reason: str = Field(..., description="风险突升原因，如发生重大医疗事故")


class InstitutionSuspendRequest(BaseModel):
    reason: str
    expected_resume_date: Optional[date] = None


class BatchRescheduleRequest(BaseModel):
    from_date: Optional[date] = None
    institution_ids: Optional[List[int]] = None
    reason: str = "批量重排未执行任务"


class BatchRescheduleResult(BaseModel):
    batch_id: str
    reason: str
    kept_count: int
    rescheduled_count: int
    gaps: List[ScheduleGapSchema]
    locked_tasks_untouched: int
    kept_task_ids: List[int]
    rescheduled_task_ids: List[int]


class RuleUpgradeResult(BaseModel):
    rule_version_id: int
    version_code: str
    open_tasks_unchanged: int
    note: str


# ComplianceScoreDetail 前向引用了本文件后文定义的 RuleVersionSchema
ComplianceScoreDetail.model_rebuild()

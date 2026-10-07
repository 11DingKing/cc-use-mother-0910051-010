from sqlalchemy import Column, Integer, String, Date, DateTime, ForeignKey, Text, Enum, Float, Boolean, text
from sqlalchemy.orm import relationship
from sqlalchemy.schema import Index
from datetime import datetime
import enum

from .database import Base


class InstitutionType(str, enum.Enum):
    CLINIC = "医疗美容诊所"
    HOSPITAL = "医疗美容医院"
    DEPARTMENT = "医院美容科"
    OUTPATIENT = "医疗美容门诊部"


class ProcedureCategory(str, enum.Enum):
    SURGERY = "手术类"
    INJECTION = "注射类"
    PHOTOELECTRIC = "光电类"
    SKINCARE = "皮肤护理类"
    ORAL = "口腔美容类"


class SurgeryLevel(str, enum.Enum):
    LEVEL_1 = "一级"
    LEVEL_2 = "二级"
    LEVEL_3 = "三级"
    LEVEL_4 = "四级"


class QualificationType(str, enum.Enum):
    DOCTOR = "医师资格证"
    PRACTICE = "医师执业证"
    NURSE = "护士执业证"
    ANESTHESIA = "麻醉医师资格证"
    COSMETOLOGY = "医疗美容主诊医师资格证"


class ClueType(str, enum.Enum):
    QUICK_TRAINING = "疑似速成班培训"
    UNLICENSED_STAFF = "无证人员上岗"
    FALSE_ADVERTISEMENT = "广告虚假宣传"
    OVER_RANGE_PRACTICE = "超范围执业"


class ClueStatus(str, enum.Enum):
    PENDING = "待分派"
    ASSIGNED = "核查中"
    VERIFIED = "已核实违规"
    DISMISSED = "已排除"


class CluePriority(str, enum.Enum):
    HIGH = "高"
    MEDIUM = "中"
    LOW = "低"


class ComplianceGrade(str, enum.Enum):
    EXCELLENT = "A"
    GOOD = "B"
    FAIR = "C"
    POOR = "D"


class ScoreItem(str, enum.Enum):
    LICENSE_VALID = "许可证有效性"
    LICENSE_COMPLETE = "许可证完整性"
    NO_OVER_RANGE = "无超范围执业"
    ALL_STAFF_LICENSED = "从业人员全部持证"
    NO_QUICK_TRAINING = "无速成班线索"
    NO_FALSE_ADVERTISEMENT = "无虚假宣传线索"
    NO_VERIFIED_VIOLATION = "无已核实违规记录"


class InspectionFrequency(str, enum.Enum):
    QUARTERLY = "每季度一次"
    BIANNUAL = "每半年一次"
    ANNUAL = "每年一次"
    EXTENDED = "每两年一次"


class PlanStatus(str, enum.Enum):
    PENDING = "待执行"
    IN_PROGRESS = "进行中"
    COMPLETED = "已完成"
    CANCELLED = "已取消"


class RiskTrend(str, enum.Enum):
    STABLE = "平稳"
    UPGRADED = "风险突升"
    DOWN_GRADED = "风险下降"


class TaskStatus(str, enum.Enum):
    SCHEDULED = "已排期"
    IN_PROGRESS = "进行中"
    COMPLETED = "已完成"
    CANCELLED = "已取消"
    POSTPONED = "已延期"
    UNSCHEDULED = "无法排期"


class TaskChangeType(str, enum.Enum):
    CREATED = "创建"
    SCHEDULED = "排期"
    RESCHEDULED = "重排"
    POSTPONED = "延期"
    STARTED = "开始执行"
    CANCELLED = "取消"
    PRIORITY_CHANGED = "优先级调整"
    KEPT = "保留"
    GAP_OPENED = "产生排期缺口"
    GAP_RESOLVED = "缺口消除"
    RESULT_RECORDED = "结果回写"


class ChangeTrigger(str, enum.Enum):
    PLAN_GENERATION = "季度计划生成"
    POSTPONE = "检查延期"
    SUSPENSION = "机构停业"
    RISK_SURGE = "风险突升"
    RESULT_WRITEBACK = "检查结果回写"
    RULE_UPGRADE = "评分规则换版"
    BATCH_RESCHEDULE = "批量重排"
    MANUAL = "人工调整"


class GapReason(str, enum.Enum):
    NO_CAPACITY = "排期窗口内执法人员容量不足"
    INSPECTOR_CONFLICT = "执法人员时间冲突且无可用替代人员"
    INSUFFICIENT_INSPECTORS = "可用执法人员不足"
    OUTSIDE_WINDOW = "无可用排期窗口"


class RuleStatus(str, enum.Enum):
    ACTIVE = "生效中"
    DEPRECATED = "已停用"


class Institution(Base):
    __tablename__ = "institutions"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False, index=True)
    unified_social_code = Column(String(50), unique=True, index=True)
    institution_type = Column(Enum(InstitutionType), nullable=False)
    legal_person = Column(String(100))
    address = Column(String(500))
    phone = Column(String(50))
    registration_date = Column(Date)
    business_scope = Column(Text)
    is_suspended = Column(Boolean, default=False, nullable=False)
    suspended_at = Column(DateTime)
    suspend_reason = Column(String(500))
    expected_resume_date = Column(Date)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    licenses = relationship("InstitutionLicense", back_populates="institution", cascade="all, delete-orphan")
    authorized_procedures = relationship("InstitutionAuthorizedProcedure", back_populates="institution", cascade="all, delete-orphan")
    practitioners = relationship("Practitioner", back_populates="institution")
    actual_procedure_records = relationship("ActualProcedureRecord", back_populates="institution")
    clues = relationship("ViolationClue", back_populates="institution")
    compliance_scores = relationship("ComplianceScore", back_populates="institution", cascade="all, delete-orphan")
    supervision_plans = relationship("SupervisionPlan", back_populates="institution", cascade="all, delete-orphan")
    inspection_tasks = relationship("InspectionTask", back_populates="institution")


class InstitutionLicense(Base):
    __tablename__ = "institution_licenses"

    id = Column(Integer, primary_key=True, index=True)
    institution_id = Column(Integer, ForeignKey("institutions.id"), nullable=False)
    license_number = Column(String(100), unique=True, nullable=False, index=True)
    issuing_authority = Column(String(200))
    issue_date = Column(Date)
    valid_until = Column(Date)
    approved_surgeries = Column(Text)
    is_valid = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    institution = relationship("Institution", back_populates="licenses")


class Practitioner(Base):
    __tablename__ = "practitioners"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), nullable=False, index=True)
    id_card = Column(String(18), unique=True, index=True)
    gender = Column(String(10))
    birth_date = Column(Date)
    institution_id = Column(Integer, ForeignKey("institutions.id"))
    position = Column(String(100))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    institution = relationship("Institution", back_populates="practitioners")
    qualifications = relationship("PractitionerQualification", back_populates="practitioner", cascade="all, delete-orphan")
    authorized_procedures = relationship("PractitionerAuthorizedProcedure", back_populates="practitioner", cascade="all, delete-orphan")
    actual_procedure_records = relationship("ActualProcedureRecord", back_populates="practitioner")


class PractitionerQualification(Base):
    __tablename__ = "practitioner_qualifications"

    id = Column(Integer, primary_key=True, index=True)
    practitioner_id = Column(Integer, ForeignKey("practitioners.id"), nullable=False)
    qualification_type = Column(Enum(QualificationType), nullable=False)
    certificate_number = Column(String(100), nullable=False, index=True)
    issuing_authority = Column(String(200))
    issue_date = Column(Date)
    valid_until = Column(Date)
    practice_scope = Column(String(500))
    is_valid = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    practitioner = relationship("Practitioner", back_populates="qualifications")


class Procedure(Base):
    __tablename__ = "procedures"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(200), nullable=False, unique=True, index=True)
    code = Column(String(50), unique=True, index=True)
    category = Column(Enum(ProcedureCategory), nullable=False)
    surgery_level = Column(Enum(SurgeryLevel))
    description = Column(Text)
    requires_qualification = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)

    institution_authorizations = relationship("InstitutionAuthorizedProcedure", back_populates="procedure")
    practitioner_authorizations = relationship("PractitionerAuthorizedProcedure", back_populates="procedure")
    actual_records = relationship("ActualProcedureRecord", back_populates="procedure")


class InstitutionAuthorizedProcedure(Base):
    __tablename__ = "institution_authorized_procedures"

    id = Column(Integer, primary_key=True, index=True)
    institution_id = Column(Integer, ForeignKey("institutions.id"), nullable=False)
    procedure_id = Column(Integer, ForeignKey("procedures.id"), nullable=False)
    authorized_date = Column(Date)
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)

    institution = relationship("Institution", back_populates="authorized_procedures")
    procedure = relationship("Procedure", back_populates="institution_authorizations")


class PractitionerAuthorizedProcedure(Base):
    __tablename__ = "practitioner_authorized_procedures"

    id = Column(Integer, primary_key=True, index=True)
    practitioner_id = Column(Integer, ForeignKey("practitioners.id"), nullable=False)
    procedure_id = Column(Integer, ForeignKey("procedures.id"), nullable=False)
    authorized_date = Column(Date)
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)

    practitioner = relationship("Practitioner", back_populates="authorized_procedures")
    procedure = relationship("Procedure", back_populates="practitioner_authorizations")


class ActualProcedureRecord(Base):
    __tablename__ = "actual_procedure_records"

    id = Column(Integer, primary_key=True, index=True)
    institution_id = Column(Integer, ForeignKey("institutions.id"), nullable=False)
    practitioner_id = Column(Integer, ForeignKey("practitioners.id"), nullable=False)
    procedure_id = Column(Integer, ForeignKey("procedures.id"), nullable=False)
    procedure_date = Column(Date, nullable=False)
    patient_count = Column(Integer, default=1)
    remark = Column(String(500))
    is_over_range = Column(Boolean, default=False)
    over_range_detail = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)

    institution = relationship("Institution", back_populates="actual_procedure_records")
    practitioner = relationship("Practitioner", back_populates="actual_procedure_records")
    procedure = relationship("Procedure", back_populates="actual_records")


class ViolationClue(Base):
    __tablename__ = "violation_clues"

    id = Column(Integer, primary_key=True, index=True)
    clue_type = Column(Enum(ClueType), nullable=False)
    title = Column(String(300), nullable=False)
    description = Column(Text, nullable=False)
    institution_id = Column(Integer, ForeignKey("institutions.id"))
    practitioner_id = Column(Integer, ForeignKey("practitioners.id"))
    procedure_id = Column(Integer, ForeignKey("procedures.id"))
    source = Column(String(200))
    priority = Column(Enum(CluePriority), default=CluePriority.MEDIUM)
    status = Column(Enum(ClueStatus), default=ClueStatus.PENDING)
    assignee = Column(String(100))
    assigned_at = Column(DateTime)
    conclusion = Column(Text)
    verified_at = Column(DateTime)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    institution = relationship("Institution", back_populates="clues")
    procedure = relationship("Procedure")
    inspection_records = relationship("InspectionRecord", back_populates="clue", cascade="all, delete-orphan")


class InspectionRecord(Base):
    __tablename__ = "inspection_records"

    id = Column(Integer, primary_key=True, index=True)
    clue_id = Column(Integer, ForeignKey("violation_clues.id"), nullable=False)
    inspector = Column(String(100), nullable=False)
    inspection_date = Column(Date, nullable=False)
    content = Column(Text, nullable=False)
    finding = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)

    clue = relationship("ViolationClue", back_populates="inspection_records")


class ComplianceScore(Base):
    __tablename__ = "compliance_scores"

    id = Column(Integer, primary_key=True, index=True)
    institution_id = Column(Integer, ForeignKey("institutions.id"), nullable=False)
    total_score = Column(Float, nullable=False, default=100.0)
    grade = Column(Enum(ComplianceGrade), nullable=False)
    license_valid_score = Column(Float, default=15.0)
    license_complete_score = Column(Float, default=10.0)
    no_over_range_score = Column(Float, default=20.0)
    all_staff_licensed_score = Column(Float, default=20.0)
    no_quick_training_score = Column(Float, default=15.0)
    no_false_advertisement_score = Column(Float, default=10.0)
    no_verified_violation_score = Column(Float, default=10.0)
    deduction_details = Column(Text)
    inspection_frequency = Column(Enum(InspectionFrequency), nullable=False)
    rule_version_id = Column(Integer, ForeignKey("rule_versions.id"))
    scored_at = Column(DateTime, default=datetime.utcnow)
    scoring_period = Column(String(50))
    remark = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    institution = relationship("Institution", back_populates="compliance_scores")
    supervision_plans = relationship("SupervisionPlan", back_populates="compliance_score", cascade="all, delete-orphan")
    rule_version = relationship("RuleVersion")
    inspection_tasks = relationship("InspectionTask", back_populates="compliance_score")


class SupervisionPlan(Base):
    __tablename__ = "supervision_plans"

    id = Column(Integer, primary_key=True, index=True)
    compliance_score_id = Column(Integer, ForeignKey("compliance_scores.id"), nullable=False)
    institution_id = Column(Integer, ForeignKey("institutions.id"), nullable=False)
    plan_title = Column(String(300), nullable=False)
    plan_content = Column(Text, nullable=False)
    planned_date = Column(Date, nullable=False)
    inspector = Column(String(100))
    status = Column(Enum(PlanStatus), default=PlanStatus.PENDING)
    priority = Column(Enum(CluePriority), default=CluePriority.MEDIUM)
    focus_areas = Column(Text)
    actual_inspection_date = Column(Date)
    result = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    compliance_score = relationship("ComplianceScore", back_populates="supervision_plans")
    institution = relationship("Institution", back_populates="supervision_plans")


class RuleVersion(Base):
    """评分依据与检查频率规则的不可变版本快照。"""
    __tablename__ = "rule_versions"

    id = Column(Integer, primary_key=True, index=True)
    version_code = Column(String(50), unique=True, nullable=False, index=True)
    status = Column(Enum(RuleStatus), default=RuleStatus.ACTIVE, nullable=False)
    effective_from = Column(Date, nullable=False)
    # 冻结的评分项满分、等级边界、等级->频率/轮次/优先级映射
    rules_snapshot = Column(Text, nullable=False)
    change_summary = Column(String(500))
    created_at = Column(DateTime, default=datetime.utcnow)
    deprecated_at = Column(DateTime)


class Inspector(Base):
    """执法人员（可被排期的有限执法资源）。"""
    __tablename__ = "inspectors"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, nullable=False, index=True)
    daily_capacity = Column(Integer, default=1, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    tasks = relationship("InspectionTask", back_populates="inspector")
    blocks = relationship("InspectorBlock", back_populates="inspector", cascade="all, delete-orphan")


class InspectorBlock(Base):
    """执法人员不可用时间（请假、公务等）。"""
    __tablename__ = "inspector_blocks"

    id = Column(Integer, primary_key=True, index=True)
    inspector_id = Column(Integer, ForeignKey("inspectors.id"), nullable=False)
    block_date = Column(Date, nullable=False)
    reason = Column(String(200))
    created_at = Column(DateTime, default=datetime.utcnow)

    inspector = relationship("Inspector", back_populates="blocks")

    __table_args__ = (
        Index("uq_inspector_block_date", "inspector_id", "block_date", unique=True),
    )


class InspectionTask(Base):
    """
    有容量约束的检查任务。生成时冻结所采用的风险评分、等级、检查频率与
    优先级快照（连同规则版本），使事后规则变化不会改写已生成任务的依据。
    """
    __tablename__ = "inspection_tasks"

    id = Column(Integer, primary_key=True, index=True)
    institution_id = Column(Integer, ForeignKey("institutions.id"), nullable=False)
    compliance_score_id = Column(Integer, ForeignKey("compliance_scores.id"))
    rule_version_id = Column(Integer, ForeignKey("rule_versions.id"), nullable=False)
    round_no = Column(Integer, nullable=False, default=1)
    quarter = Column(String(20), nullable=False, index=True)

    # 冻结的生成依据
    frozen_score = Column(Float, nullable=False)
    frozen_grade = Column(Enum(ComplianceGrade), nullable=False)
    frozen_frequency = Column(Enum(InspectionFrequency), nullable=False)
    priority = Column(Enum(CluePriority), nullable=False, default=CluePriority.MEDIUM)

    title = Column(String(300), nullable=False)
    plan_content = Column(Text)
    focus_areas = Column(Text)

    earliest_date = Column(Date, nullable=False)
    due_date = Column(Date, nullable=False)
    scheduled_date = Column(Date, index=True)
    inspector_id = Column(Integer, ForeignKey("inspectors.id"), index=True)
    status = Column(Enum(TaskStatus), nullable=False, default=TaskStatus.SCHEDULED, index=True)

    actual_inspection_date = Column(Date)
    result = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    institution = relationship("Institution", back_populates="inspection_tasks")
    compliance_score = relationship("ComplianceScore", back_populates="inspection_tasks")
    rule_version = relationship("RuleVersion")
    inspector = relationship("Inspector", back_populates="tasks")
    change_logs = relationship("TaskChangeLog", back_populates="task", cascade="all, delete-orphan")
    schedule_gap = relationship("ScheduleGap", back_populates="task", uselist=False, cascade="all, delete-orphan")

    __table_args__ = (
        Index(
            "uq_institution_open_task",
            "institution_id", "quarter", "round_no",
            unique=True,
            sqlite_where=text("status NOT IN ('COMPLETED', 'CANCELLED')"),
            postgresql_where=text("status NOT IN ('COMPLETED', 'CANCELLED')"),
        ),
    )


class TaskChangeLog(Base):
    """任务生命周期事件审计轨迹：保留/取消/重排及优先级变化原因。"""
    __tablename__ = "task_change_logs"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("inspection_tasks.id"), nullable=False, index=True)
    change_type = Column(Enum(TaskChangeType), nullable=False)
    trigger = Column(Enum(ChangeTrigger), nullable=False)
    reason = Column(Text, nullable=False)
    old_status = Column(Enum(TaskStatus))
    new_status = Column(Enum(TaskStatus))
    old_priority = Column(Enum(CluePriority))
    new_priority = Column(Enum(CluePriority))
    old_scheduled_date = Column(Date)
    new_scheduled_date = Column(Date)
    old_inspector_id = Column(Integer, ForeignKey("inspectors.id"))
    new_inspector_id = Column(Integer, ForeignKey("inspectors.id"))
    batch_id = Column(String(40), index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("InspectionTask", back_populates="change_logs")


class ScheduleGap(Base):
    """无法安排的任务缺口：显式保留，绝不静默丢失。"""
    __tablename__ = "schedule_gaps"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("inspection_tasks.id"), nullable=False, unique=True)
    institution_id = Column(Integer, ForeignKey("institutions.id"), nullable=False)
    reason = Column(Enum(GapReason), nullable=False)
    detail = Column(Text, nullable=False)
    window_start = Column(Date, nullable=False)
    window_end = Column(Date, nullable=False)
    required_capacity = Column(Integer, default=1)
    resolved = Column(Boolean, default=False, nullable=False)
    resolved_at = Column(DateTime)
    batch_id = Column(String(40), index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    task = relationship("InspectionTask", back_populates="schedule_gap")

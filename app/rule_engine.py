"""
评分依据与检查频率规则的版本化管理。

专项整治期间评分口径与检查频率会调整，而季度检查计划必须固化"生成时所采用
的依据"。RuleVersion 以 JSON 快照形式冻结：
  - 各评分项满分
  - 等级分数线
  - 等级 -> 检查频率 / 年度检查轮次 / 优先级 映射
  - 频率间隔月数、排期窗口天数
历史版本不可变；换版通过创建新版本并停用旧版本完成，已生成的评分与检查任务
仍指向其生成时的版本，不被新规则改写。
"""
import json
from datetime import date, datetime
from typing import Dict, Optional

from sqlalchemy.orm import Session

from .models import (
    RuleVersion, RuleStatus, ScoreItem, ComplianceGrade,
    InspectionFrequency, CluePriority,
)

# 首版规则编码：专项整治首版评分依据
INITIAL_VERSION_CODE = "2024Q1-v1"

# 排期窗口（任务最早可检查日 ~ 最晚应完成日）的默认天数
DEFAULT_WINDOW_DAYS = 30


def _serialize_enum_map(mapping: Dict) -> Dict:
    return {key.value: value for key, value in mapping.items()}


def build_v1_rules() -> dict:
    """首版规则（与系统初始化时的口径保持一致）。"""
    return {
        "version_code": INITIAL_VERSION_CODE,
        "max_scores": {
            ScoreItem.LICENSE_VALID.value: 15.0,
            ScoreItem.LICENSE_COMPLETE.value: 10.0,
            ScoreItem.NO_OVER_RANGE.value: 20.0,
            ScoreItem.ALL_STAFF_LICENSED.value: 20.0,
            ScoreItem.NO_QUICK_TRAINING.value: 15.0,
            ScoreItem.NO_FALSE_ADVERTISEMENT.value: 10.0,
            ScoreItem.NO_VERIFIED_VIOLATION.value: 10.0,
        },
        "grade_ranges": [
            [ComplianceGrade.EXCELLENT.value, 90.0, 100.0],
            [ComplianceGrade.GOOD.value, 75.0, 89.99],
            [ComplianceGrade.FAIR.value, 60.0, 74.99],
            [ComplianceGrade.POOR.value, 0.0, 59.99],
        ],
        "grade_frequency": {
            ComplianceGrade.EXCELLENT.value: InspectionFrequency.EXTENDED.value,
            ComplianceGrade.GOOD.value: InspectionFrequency.ANNUAL.value,
            ComplianceGrade.FAIR.value: InspectionFrequency.BIANNUAL.value,
            ComplianceGrade.POOR.value: InspectionFrequency.QUARTERLY.value,
        },
        # 各等级在一个12个月计划周期内的检查轮次
        "grade_rounds": {
            ComplianceGrade.EXCELLENT.value: 1,
            ComplianceGrade.GOOD.value: 2,
            ComplianceGrade.FAIR.value: 3,
            ComplianceGrade.POOR.value: 4,
        },
        "grade_priority": {
            ComplianceGrade.EXCELLENT.value: CluePriority.LOW.value,
            ComplianceGrade.GOOD.value: CluePriority.LOW.value,
            ComplianceGrade.FAIR.value: CluePriority.MEDIUM.value,
            ComplianceGrade.POOR.value: CluePriority.HIGH.value,
        },
        "frequency_months": {
            InspectionFrequency.QUARTERLY.value: 3,
            InspectionFrequency.BIANNUAL.value: 6,
            InspectionFrequency.ANNUAL.value: 12,
            InspectionFrequency.EXTENDED.value: 24,
        },
        "window_days": DEFAULT_WINDOW_DAYS,
    }


def serialize_rules(rules: dict) -> str:
    return json.dumps(rules, ensure_ascii=False, sort_keys=True)


def load_rules(rule_version: RuleVersion) -> dict:
    return json.loads(rule_version.rules_snapshot)


def get_active_rule_version(db: Session) -> RuleVersion:
    """返回当前生效版本；若系统中尚不存在任何版本则初始化首版。"""
    active = db.query(RuleVersion).filter(
        RuleVersion.status == RuleStatus.ACTIVE
    ).order_by(RuleVersion.id.desc()).first()
    if active:
        return active

    existing = db.query(RuleVersion).order_by(RuleVersion.id.asc()).first()
    if existing:
        existing.status = RuleStatus.ACTIVE
        db.flush()
        return existing

    return create_rule_version(
        db,
        version_code=INITIAL_VERSION_CODE,
        effective_from=date.today(),
        change_summary="初始化首版评分依据与检查频率规则",
        rules=build_v1_rules(),
        flush_only=True,
    )


def create_rule_version(
    db: Session,
    version_code: str,
    effective_from: date,
    change_summary: str = "",
    rules: Optional[dict] = None,
    flush_only: bool = False,
) -> RuleVersion:
    """创建并启用新版本，同时停用旧版本（历史快照保持不可变）。"""
    duplicate = db.query(RuleVersion).filter(
        RuleVersion.version_code == version_code
    ).first()
    if duplicate:
        raise ValueError(f"规则版本 {version_code} 已存在")

    base = load_rules(get_active_rule_version(db)) if db.query(RuleVersion).count() else build_v1_rules()
    merged = {**base, **(rules or {}), "version_code": version_code}

    old_actives = db.query(RuleVersion).filter(
        RuleVersion.status == RuleStatus.ACTIVE
    ).all()
    now = datetime.utcnow()
    for old in old_actives:
        old.status = RuleStatus.DEPRECATED
        old.deprecated_at = now

    rule_version = RuleVersion(
        version_code=version_code,
        status=RuleStatus.ACTIVE,
        effective_from=effective_from,
        rules_snapshot=serialize_rules(merged),
        change_summary=change_summary,
    )
    db.add(rule_version)
    db.flush()
    if not flush_only:
        db.commit()
        db.refresh(rule_version)
    return rule_version


def get_rule_version(db: Session, version_id: Optional[int] = None) -> RuleVersion:
    if version_id is not None:
        rule_version = db.query(RuleVersion).filter(RuleVersion.id == version_id).first()
        if not rule_version:
            raise ValueError(f"规则版本 {version_id} 不存在")
        return rule_version
    return get_active_rule_version(db)


# ---- 规则快照读取辅助 ----

def max_scores_of(rules: dict) -> Dict[ScoreItem, float]:
    return {ScoreItem(k): v for k, v in rules["max_scores"].items()}


def grade_of(rules: dict, total_score: float) -> ComplianceGrade:
    for grade_value, min_score, max_score in rules["grade_ranges"]:
        if min_score <= total_score <= max_score:
            return ComplianceGrade(grade_value)
    return ComplianceGrade.POOR


def frequency_of(rules: dict, grade: ComplianceGrade) -> InspectionFrequency:
    return InspectionFrequency(rules["grade_frequency"][grade.value])


def rounds_of(rules: dict, grade: ComplianceGrade) -> int:
    return int(rules["grade_rounds"][grade.value])


def priority_of(rules: dict, grade: ComplianceGrade) -> CluePriority:
    return CluePriority(rules["grade_priority"][grade.value])


def frequency_months_of(rules: dict, frequency: InspectionFrequency) -> int:
    return int(rules["frequency_months"][frequency.value])


def window_days_of(rules: dict) -> int:
    return int(rules.get("window_days", DEFAULT_WINDOW_DAYS))

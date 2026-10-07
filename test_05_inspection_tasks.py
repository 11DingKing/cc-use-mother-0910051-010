# -*- coding: utf-8 -*-
"""容量约束检查任务：版本冻结、容量排期、事件重排、缺口与审计测试。"""
from datetime import date, timedelta

import pytest
from sqlalchemy.exc import IntegrityError

from app.models import (
    Institution, InstitutionType, ComplianceScore, ComplianceGrade,
    InspectionFrequency, CluePriority, Inspector, InspectionTask,
    TaskStatus, TaskChangeType, ChangeTrigger, ScheduleGap, TaskChangeLog,
)
from app import task_service
from app.rule_engine import (
    get_active_rule_version, load_rules, create_rule_version, build_v1_rules,
    frequency_of, rounds_of,
)


def _make_institution(db, code, name=None, suspended=False):
    inst = Institution(
        name=name or f"任务测试机构-{code}",
        unified_social_code=f"91310000TASK{code}",
        institution_type=InstitutionType.CLINIC,
        is_suspended=suspended,
    )
    db.add(inst)
    db.flush()
    return inst


def _make_score(db, inst, grade, total_score, *, rule_version=None):
    rule_version = rule_version or get_active_rule_version(db)
    rules = load_rules(rule_version)
    score = ComplianceScore(
        institution_id=inst.id,
        total_score=total_score,
        grade=grade,
        inspection_frequency=frequency_of(rules, grade),
        rule_version_id=rule_version.id,
        scoring_period=f"{date.today().year}年专项整治",
    )
    db.add(score)
    db.flush()
    return score


def _make_inspectors(db, n, prefix, capacity=1):
    inspectors = []
    for i in range(n):
        ins = Inspector(name=f"{prefix}-执法员{i+1}", daily_capacity=capacity)
        db.add(ins)
        inspectors.append(ins)
    db.flush()
    return inspectors


GRADE_SCORE = {
    ComplianceGrade.POOR: 40.0,
    ComplianceGrade.FAIR: 65.0,
    ComplianceGrade.GOOD: 80.0,
    ComplianceGrade.EXCELLENT: 95.0,
}


@pytest.fixture
def inspectors(db_session):
    return _make_inspectors(db_session, 2, "基础")


@pytest.fixture
def four_grade_institutions(db_session):
    """A/B/C/D 四级机构各一家。"""
    mapping = {}
    for idx, grade in enumerate([
        ComplianceGrade.EXCELLENT, ComplianceGrade.GOOD,
        ComplianceGrade.FAIR, ComplianceGrade.POOR
    ]):
        inst = _make_institution(db_session, f"G{idx}")
        score = _make_score(db_session, inst, grade, GRADE_SCORE[grade])
        mapping[grade] = (inst, score)
    db_session.flush()
    return mapping


class TestRuleVersionFreezing:
    def test_score_and_tasks_freeze_rule_version(self, db_session, inspectors, four_grade_institutions):
        """生成时固定风险评分、等级、频率、优先级与规则版本。"""
        v1 = get_active_rule_version(db_session)
        result = task_service.generate_quarterly_plan(db_session, start_date=date.today())

        assert result["rule_version_code"] == v1.version_code
        # A:1 B:2 C:3 D:4
        tasks = db_session.query(InspectionTask).all()
        assert len(tasks) == 10
        for grade, (inst, score) in four_grade_institutions.items():
            inst_tasks = [t for t in tasks if t.institution_id == inst.id]
            assert len(inst_tasks) == rounds_of(load_rules(v1), grade)
            for t in inst_tasks:
                assert t.rule_version_id == v1.id
                assert t.compliance_score_id == score.id
                assert t.frozen_score == GRADE_SCORE[grade]
                assert t.frozen_grade == grade
                assert t.frozen_frequency == score.inspection_frequency
        # D 级冻结为高优先级，C 级为中
        d_inst = four_grade_institutions[ComplianceGrade.POOR][0]
        c_inst = four_grade_institutions[ComplianceGrade.FAIR][0]
        assert all(t.priority == CluePriority.HIGH for t in tasks if t.institution_id == d_inst.id)
        assert all(t.priority == CluePriority.MEDIUM for t in tasks if t.institution_id == c_inst.id)
        # 每个任务都有创建审计日志，写明评分依据
        logs = db_session.query(TaskChangeLog).filter(
            TaskChangeLog.change_type == TaskChangeType.CREATED
        ).all()
        assert len(logs) == 10
        assert any(v1.version_code in l.reason for l in logs)

    def test_new_rule_version_does_not_rewrite_existing_tasks(self, db_session, inspectors, four_grade_institutions):
        """换版后已生成任务保留旧版本冻结快照；新计划采用新版本。"""
        result = task_service.generate_quarterly_plan(db_session, start_date=date.today())
        old_tasks = db_session.query(InspectionTask).all()
        old_snapshot = [(t.id, t.frozen_frequency, t.rule_version_id, t.priority) for t in old_tasks]

        # 新版本：B 级也每季度检查、窗口缩短
        new_rules = {
            "window_days": 15,
            "grade_frequency": {
                **build_v1_rules()["grade_frequency"],
                ComplianceGrade.GOOD.value: InspectionFrequency.QUARTERLY.value,
            },
            "grade_rounds": {
                **build_v1_rules()["grade_rounds"],
                ComplianceGrade.GOOD.value: 4,
            },
        }
        v2 = create_rule_version(
            db_session, version_code="2026Q4-v2", effective_from=date.today(),
            change_summary="专项整治加严：良好机构提高至每季度检查", rules=new_rules,
        )

        # 既有任务一个字节都不变
        db_session.expire_all()
        for task_id, freq, version_id, prio in old_snapshot:
            t = db_session.query(InspectionTask).get(task_id)
            assert (t.id, t.frozen_frequency, t.rule_version_id, t.priority) == (task_id, freq, version_id, prio)

        # 新机构按 v2 生成：B 级 4 轮、季度频率
        inst = _make_institution(db_session, "NEWB")
        score = _make_score(
            db_session, inst, ComplianceGrade.GOOD, 80.0, rule_version=v2
        )
        result2 = task_service.generate_quarterly_plan(
            db_session, start_date=date.today(), institution_ids=[inst.id]
        )
        new_tasks = result2["tasks"]
        assert len(new_tasks) == 4
        assert all(t.rule_version_id == v2.id for t in new_tasks)
        assert all(t.frozen_frequency == InspectionFrequency.QUARTERLY for t in new_tasks)


class TestCapacityAndPriority:
    def test_high_risk_scheduled_first(self, db_session, four_grade_institutions):
        """容量竞争时高风险（D/高优先级）先占最早日期。"""
        _make_inspectors(db_session, 1, "优先级", capacity=1)
        task_service.generate_quarterly_plan(db_session, start_date=date.today())
        tasks = db_session.query(InspectionTask).filter(
            InspectionTask.status == TaskStatus.SCHEDULED
        ).all()
        first_round = {}
        for grade, (inst, _) in four_grade_institutions.items():
            first = min(
                t.scheduled_date for t in tasks
                if t.institution_id == inst.id and t.round_no == 1
            )
            first_round[grade] = first
        # 唯一执法员的首日被 D 级首轮占走，其余等级依次后移
        assert first_round[ComplianceGrade.POOR] == date.today()
        assert (first_round[ComplianceGrade.POOR]
                <= first_round[ComplianceGrade.FAIR]
                <= first_round[ComplianceGrade.GOOD]
                <= first_round[ComplianceGrade.EXCELLENT])
        day_zero_tasks = [t for t in tasks if t.scheduled_date == date.today()]
        assert len(day_zero_tasks) == 1
        assert day_zero_tasks[0].priority == CluePriority.HIGH
        assert day_zero_tasks[0].frozen_grade == ComplianceGrade.POOR

    def test_inspector_daily_capacity_enforced(self, db_session):
        """同一执法员同日任务数不得超过日容量。"""
        _make_inspectors(db_session, 1, "容量", capacity=2)
        for i in range(3):
            inst = _make_institution(db_session, f"C{i}")
            _make_score(db_session, inst, ComplianceGrade.POOR, 40.0)
        result = task_service.generate_quarterly_plan(db_session, start_date=date.today())
        scheduled = [t for t in result["tasks"] if t.status == TaskStatus.SCHEDULED]
        # 第一天容量 2，第三个任务排到次日
        day0 = [t for t in scheduled if t.scheduled_date == date.today()]
        day1 = [t for t in scheduled if t.scheduled_date == date.today() + timedelta(days=1)]
        assert len(day0) == 2
        assert len(day1) == 1
        inspector_ids_day0 = {t.inspector_id for t in day0}
        assert len(inspector_ids_day0) == 1

    def test_blocked_inspector_not_assigned(self, db_session):
        """执法人员请假日期不可排期，任务分给其他人。"""
        ins1, ins2 = _make_inspectors(db_session, 2, "请假")
        # ins1 整个第一窗口请假
        for d in range(31):
            db_session.add(__import__("app.models", fromlist=["InspectorBlock"]).InspectorBlock(
                inspector_id=ins1.id, block_date=date.today() + timedelta(days=d),
                reason="年假"))
        db_session.flush()
        inst = _make_institution(db_session, "BLK")
        _make_score(db_session, inst, ComplianceGrade.POOR, 40.0)
        result = task_service.generate_quarterly_plan(db_session, start_date=date.today())
        task = result["tasks"][0]
        assert task.status == TaskStatus.SCHEDULED
        assert task.inspector_id == ins2.id

    def test_unschedulable_task_leaves_explicit_gap(self, db_session):
        """窗口内容量不足时任务不丢失：UNSCHEDULED + ScheduleGap。"""
        _make_inspectors(db_session, 1, "缺口", capacity=1)
        # 新版本窗口仅 1 天（每天 1 容量），三家 D 级机构首轮同日竞争 -> 必生缺口
        v2 = create_rule_version(
            db_session, version_code="2026Q4-v2-gap", effective_from=date.today(),
            change_summary="窗口压缩测试", rules={"window_days": 1},
        )
        for i in range(3):
            inst = _make_institution(db_session, f"GAP{i}")
            _make_score(db_session, inst, ComplianceGrade.POOR, 40.0, rule_version=v2)
        result = task_service.generate_quarterly_plan(
            db_session, start_date=date.today(), rule_version_id=v2.id
        )
        unscheduled = [t for t in result["tasks"] if t.status == TaskStatus.UNSCHEDULED]
        assert len(unscheduled) >= 1
        assert len(result["gaps"]) >= 1
        gap = result["gaps"][0]
        assert gap.resolved is False
        assert gap.task_id in {t.id for t in unscheduled}
        assert "容量" in gap.detail or "冲突" in gap.detail or "执法人员" in gap.detail

        # 增加执法人员后批量重排，缺口被显式消除而非重建丢失
        _make_inspectors(db_session, 2, "缺口增援", capacity=1)
        re = task_service.batch_reschedule(db_session, reason="增援后批量重排")
        db_session.refresh(gap)
        assert gap.resolved is True
        assert re["gaps"] == []
        db_session.expire_all()
        assert db_session.query(InspectionTask).filter(
            InspectionTask.status == TaskStatus.UNSCHEDULED
        ).count() == 0

    def test_no_inspectors_gap_reason(self, db_session, four_grade_institutions):
        """没有任何执法人员时明确报告人员不足缺口。"""
        result = task_service.generate_quarterly_plan(db_session, start_date=date.today())
        assert result["tasks_scheduled"] == 0
        assert len(result["gaps"]) == 10
        assert all(g.detail for g in result["gaps"])


class TestDuplicateTasks:
    def test_duplicate_generation_skipped_in_transaction(self, db_session, inspectors, four_grade_institutions):
        """重复季度计划不产生重复任务，逐机构给出跳过原因。"""
        r1 = task_service.generate_quarterly_plan(db_session, start_date=date.today())
        assert r1["tasks_created"] == 10
        r2 = task_service.generate_quarterly_plan(db_session, start_date=date.today())
        assert r2["tasks_created"] == 0
        assert len(r2["skipped_duplicates"]) == 4
        assert db_session.query(InspectionTask).count() == 10

    def test_database_level_open_task_unique_constraint(self, db_session, inspectors, four_grade_institutions):
        """数据库部分唯一索引兜底：同机构同季度同轮次只允许一个未终结任务。"""
        task_service.generate_quarterly_plan(db_session, start_date=date.today())
        d_inst = four_grade_institutions[ComplianceGrade.POOR][0]
        existing = db_session.query(InspectionTask).filter(
            InspectionTask.institution_id == d_inst.id
        ).order_by(InspectionTask.round_no.asc()).first()

        dup = InspectionTask(
            institution_id=d_inst.id,
            compliance_score_id=existing.compliance_score_id,
            rule_version_id=existing.rule_version_id,
            round_no=existing.round_no,
            quarter=existing.quarter,
            frozen_score=40.0,
            frozen_grade=ComplianceGrade.POOR,
            frozen_frequency=InspectionFrequency.QUARTERLY,
            priority=CluePriority.HIGH,
            title="重复任务",
            earliest_date=existing.earliest_date,
            due_date=existing.due_date,
            status=TaskStatus.SCHEDULED,
        )
        # 用 SAVEPOINT 隔离唯一约束冲突，避免污染测试的连接级事务
        with pytest.raises(IntegrityError):
            with db_session.begin_nested():
                db_session.add(dup)
                db_session.flush()

        # 已完成任务后允许同轮次存在新的开放任务（历史保留，不冲突）
        existing.status = TaskStatus.COMPLETED
        db_session.flush()
        db_session.add(InspectionTask(
            institution_id=d_inst.id,
            compliance_score_id=existing.compliance_score_id,
            rule_version_id=existing.rule_version_id,
            round_no=existing.round_no,
            quarter=existing.quarter,
            frozen_score=40.0,
            frozen_grade=ComplianceGrade.POOR,
            frozen_frequency=InspectionFrequency.QUARTERLY,
            priority=CluePriority.HIGH,
            title="补排任务",
            earliest_date=existing.earliest_date,
            due_date=existing.due_date,
            status=TaskStatus.SCHEDULED,
            scheduled_date=date.today(),
            inspector_id=inspectors[0].id,
        ))
        db_session.flush()


class TestPostpone:
    def test_postpone_releases_and_reschedules_with_audit(self, db_session):
        ins1, ins2 = _make_inspectors(db_session, 2, "延期")
        inst_a = _make_institution(db_session, "PA")
        inst_b = _make_institution(db_session, "PB")
        _make_score(db_session, inst_a, ComplianceGrade.POOR, 40.0)
        _make_score(db_session, inst_b, ComplianceGrade.POOR, 40.0)
        result = task_service.generate_quarterly_plan(
            db_session, start_date=date.today(),
            institution_ids=[inst_a.id, inst_b.id],
        )
        task_a = next(t for t in result["tasks"]
                      if t.institution_id == inst_a.id and t.round_no == 1)
        original_day, original_ins = task_a.scheduled_date, task_a.inspector_id
        new_day = date.today() + timedelta(days=60)

        task_service.postpone_task(db_session, task_a.id, new_date=new_day, reason="机构装修")

        db_session.refresh(task_a)
        assert task_a.scheduled_date == new_day
        types = {l.change_type for l in task_a.change_logs}
        assert TaskChangeType.POSTPONED in types
        assert TaskChangeType.RESCHEDULED in types
        postpone_log = next(l for l in task_a.change_logs if l.change_type == TaskChangeType.POSTPONED)
        assert "机构装修" in postpone_log.reason
        # 释放出的首日容量可被其他任务占用
        task_b = next(t for t in db_session.query(InspectionTask).all()
                      if t.institution_id == inst_b.id and t.round_no == 1)
        assert task_b.scheduled_date <= new_day

    def test_postpone_completed_task_rejected(self, db_session, inspectors):
        inst = _make_institution(db_session, "PC")
        _make_score(db_session, inst, ComplianceGrade.POOR, 40.0)
        result = task_service.generate_quarterly_plan(db_session, start_date=date.today())
        task = result["tasks"][0]
        task_service.record_task_result(db_session, task.id, result="检查正常，未发现违规")
        with pytest.raises(task_service.TaskLockedError):
            task_service.postpone_task(db_session, task.id, new_date=date.today() + timedelta(days=9))

    def test_in_progress_task_locked(self, db_session, inspectors):
        """进行中任务同样不能被延期/重排改写，但可回写结果完成。"""
        inst = _make_institution(db_session, "IP")
        _make_score(db_session, inst, ComplianceGrade.POOR, 40.0)
        result = task_service.generate_quarterly_plan(db_session, start_date=date.today())
        task = result["tasks"][0]
        task_service.mark_task_in_progress(db_session, task.id)
        with pytest.raises(task_service.TaskLockedError):
            task_service.postpone_task(db_session, task.id, new_date=date.today() + timedelta(days=9))
        # 进行中 -> 回写结果完成
        task_service.record_task_result(db_session, task.id, result="检查完毕")
        db_session.refresh(task)
        assert task.status == TaskStatus.COMPLETED
        assert any(l.change_type == TaskChangeType.STARTED for l in task.change_logs)


class TestSuspension:
    def test_suspend_cancels_open_keeps_executed(self, db_session, inspectors):
        inst = _make_institution(db_session, "SUS")
        _make_score(db_session, inst, ComplianceGrade.POOR, 40.0)
        result = task_service.generate_quarterly_plan(db_session, start_date=date.today())
        tasks = result["tasks"]
        first, rest = tasks[0], tasks[1:]

        task_service.record_task_result(db_session, first.id, result="已查处并责令整改")
        outcome = task_service.suspend_institution(
            db_session, inst.id, reason="吊销执业许可证，停业整顿")

        assert first.id in outcome["locked_tasks_kept"] or first.id not in outcome["cancelled_task_ids"]
        assert first.id not in outcome["cancelled_task_ids"]
        db_session.expire_all()
        assert db_session.query(InspectionTask).get(first.id).status == TaskStatus.COMPLETED
        for t in rest:
            assert db_session.query(InspectionTask).get(t.id).status == TaskStatus.CANCELLED
        cancel_log = next(l for l in db_session.query(InspectionTask).get(rest[0].id).change_logs
                          if l.change_type == TaskChangeType.CANCELLED)
        assert "停业" in cancel_log.reason
        assert cancel_log.trigger == ChangeTrigger.SUSPENSION

    def test_suspended_institution_skipped_in_generation(self, db_session, inspectors):
        inst = _make_institution(db_session, "SUS2", suspended=True)
        _make_score(db_session, inst, ComplianceGrade.POOR, 40.0)
        result = task_service.generate_quarterly_plan(db_session, start_date=date.today())
        assert result["tasks_created"] == 0
        assert len(result["skipped_suspended"]) == 1


class TestRiskSurge:
    def test_surge_upgrades_priority_and_moves_forward(self, db_session):
        _make_inspectors(db_session, 1, "突升", capacity=1)
        inst_surge = _make_institution(db_session, "RS")
        inst_normal = _make_institution(db_session, "RN")
        _make_score(db_session, inst_surge, ComplianceGrade.FAIR, 65.0)
        _make_score(db_session, inst_normal, ComplianceGrade.FAIR, 65.0)
        result = task_service.generate_quarterly_plan(
            db_session, start_date=date.today(),
            institution_ids=[inst_surge.id, inst_normal.id],
        )
        surge_task = next(t for t in result["tasks"]
                          if t.institution_id == inst_surge.id and t.round_no == 1)
        normal_task = next(t for t in result["tasks"]
                           if t.institution_id == inst_normal.id and t.round_no == 1)
        # 生成时两家同级，顺序不确定；风险突升后重排，突升机构必须排在前面
        task_service.handle_risk_surge(db_session, inst_surge.id, reason="发生三级医疗事故")
        db_session.refresh(surge_task)
        db_session.refresh(normal_task)
        assert surge_task.priority == CluePriority.HIGH
        assert surge_task.scheduled_date <= normal_task.scheduled_date
        plog = next(l for l in surge_task.change_logs
                    if l.change_type == TaskChangeType.PRIORITY_CHANGED)
        assert plog.old_priority == CluePriority.MEDIUM
        assert plog.new_priority == CluePriority.HIGH
        assert "三级医疗事故" in plog.reason
        assert plog.trigger == ChangeTrigger.RISK_SURGE


class TestResultWritebackLock:
    def test_completed_task_immune_to_reschedule_and_rules(self, db_session, inspectors, four_grade_institutions):
        result = task_service.generate_quarterly_plan(db_session, start_date=date.today())
        target = sorted(result["tasks"], key=lambda t: t.id)[0]
        task_service.record_task_result(
            db_session, target.id, result="发现违规，立案查处",
            inspection_date=date.today())
        frozen = (target.scheduled_date, target.inspector_id, target.frozen_score,
                  target.priority, target.rule_version_id)

        # 任何重排都不能改动它
        re = task_service.batch_reschedule(db_session, reason="新一季度批量重排")
        assert re["locked_tasks_untouched"] >= 1
        db_session.refresh(target)
        assert (target.scheduled_date, target.inspector_id, target.frozen_score,
                target.priority, target.rule_version_id) == frozen
        assert target.status == TaskStatus.COMPLETED
        assert target.actual_inspection_date == date.today()

        # 重复回写被拒
        with pytest.raises(task_service.TaskLockedError):
            task_service.record_task_result(db_session, target.id, result="重复回写")

        # 结果回写有审计日志
        rlog = next(l for l in target.change_logs
                    if l.change_type == TaskChangeType.RESULT_RECORDED)
        assert "立案查处" in rlog.reason

    def test_result_writeback_reassigns_released_capacity(self, db_session):
        """完成检查后释放的容量在同事务内重排给其他机构的高优先级任务。"""
        _make_inspectors(db_session, 1, "回写", capacity=1)
        inst_a = _make_institution(db_session, "RWA")
        inst_b = _make_institution(db_session, "RWB")
        _make_score(db_session, inst_a, ComplianceGrade.POOR, 40.0)
        _make_score(db_session, inst_b, ComplianceGrade.POOR, 40.0)
        result = task_service.generate_quarterly_plan(
            db_session, start_date=date.today(),
            institution_ids=[inst_a.id, inst_b.id])
        a_first = next(t for t in result["tasks"]
                       if t.institution_id == inst_a.id and t.round_no == 1)
        b_first = next(t for t in result["tasks"]
                       if t.institution_id == inst_b.id and t.round_no == 1)
        day_after_a = a_first.scheduled_date + timedelta(days=1)
        assert b_first.scheduled_date >= day_after_a  # 排在 A 之后

        # 回写 A 的首轮结果后，B 首轮在同事务内前移填补释放出的容量
        task_service.record_task_result(db_session, a_first.id, result="检查完成")
        db_session.refresh(b_first)
        assert b_first.scheduled_date == a_first.scheduled_date
        triggers = {l.trigger for l in b_first.change_logs}
        assert ChangeTrigger.RESULT_WRITEBACK in triggers


class TestBatchReschedule:
    def test_kept_moved_gap_and_locked_classification(self, db_session):
        ins1, = _make_inspectors(db_session, 1, "批量", capacity=1)
        inst_a = _make_institution(db_session, "BA")
        inst_b = _make_institution(db_session, "BB")
        _make_score(db_session, inst_a, ComplianceGrade.POOR, 40.0)
        _make_score(db_session, inst_b, ComplianceGrade.FAIR, 65.0)
        result = task_service.generate_quarterly_plan(
            db_session, start_date=date.today(),
            institution_ids=[inst_a.id, inst_b.id],
        )
        # 先完成 A 的首轮任务（锁定）
        a_first = next(t for t in result["tasks"] if t.institution_id == inst_a.id and t.round_no == 1)
        task_service.record_task_result(db_session, a_first.id, result="已检查")

        # 让 ins1 在 B 首轮任务当天不可用，重排应把该任务前移/后移，
        # 并显式区分保留/重排
        b_first = next(t for t in result["tasks"] if t.institution_id == inst_b.id and t.round_no == 1)
        blocked_day = b_first.scheduled_date
        from app.models import InspectorBlock
        db_session.add(InspectorBlock(
            inspector_id=ins1.id, block_date=blocked_day, reason="公务"))
        db_session.flush()

        re = task_service.batch_reschedule(db_session, reason="执法员公务冲突后重排")
        kept_ids = [t.id for t in re["kept"]]
        moved_ids = [t.id for t in re["rescheduled"]]
        assert re["locked_tasks_untouched"] == 1
        assert a_first.id not in kept_ids + moved_ids
        # 至少有一个任务被移动，且所有未锁任务仍有安排
        assert len(moved_ids) >= 1
        moved = db_session.query(InspectionTask).get(moved_ids[0])
        # 已完成任务释放出的 D0 容量被复用，任务离开了被阻塞的日期
        assert moved.scheduled_date != blocked_day
        assert moved.scheduled_date is not None
        log = [l for l in moved.change_logs if l.change_type == TaskChangeType.RESCHEDULED][-1]
        assert log.batch_id == re["batch_id"]

    def test_individual_unschedulable_gap_preserved_in_batch(self, db_session):
        """批量重排中个别任务排不进去时保留明确缺口，其余照常。"""
        _make_inspectors(db_session, 1, "批量缺口", capacity=1)
        # 极短窗口版本制造容量缺口：窗口含首尾共2天，3家机构首轮竞争2个槽位
        v2 = create_rule_version(
            db_session, version_code="2026Q4-v2-batchgap", effective_from=date.today(),
            change_summary="窗口1天", rules={"window_days": 1})
        for i in range(3):
            inst = _make_institution(db_session, f"BG{i}")
            _make_score(db_session, inst, ComplianceGrade.POOR, 40.0, rule_version=v2)
        gen = task_service.generate_quarterly_plan(
            db_session, start_date=date.today(), rule_version_id=v2.id)
        scheduled_before = [t for t in gen["tasks"] if t.status == TaskStatus.SCHEDULED]
        gap_before = [t for t in gen["tasks"] if t.status == TaskStatus.UNSCHEDULED]
        assert len(scheduled_before) >= 1 and len(gap_before) >= 1

        re = task_service.batch_reschedule(db_session, reason="再次批量重排")
        # 已排上的任务依然保留/可重排，缺口任务依旧显式挂账，没有静默消失
        assert len(re["gaps"]) == len(gap_before)
        all_tasks = db_session.query(InspectionTask).all()
        assert len(all_tasks) == len(scheduled_before) + len(gap_before)
        assert db_session.query(ScheduleGap).filter(ScheduleGap.resolved == False).count() == len(gap_before)  # noqa: E712


class TestTaskAPI:
    def test_full_event_chain_via_api(self, client, db_session):
        # 配置执法人员
        for name in ["API-钱队", "API-孙队"]:
            r = client.post("/api/inspection-tasks/inspectors", json={"name": name, "daily_capacity": 1})
            assert r.status_code == 200

        inst = _make_institution(db_session, "API")
        _make_score(db_session, inst, ComplianceGrade.FAIR, 65.0)

        # 生成季度计划
        r = client.post("/api/inspection-tasks/quarterly-plan", json={
            "start_date": date.today().isoformat(),
            "institution_ids": [inst.id],
        })
        assert r.status_code == 200
        body = r.json()
        assert body["tasks_created"] == 3
        assert body["tasks_scheduled"] == 3
        assert body["skipped_duplicates"] == []

        task_id = body["tasks"][0]["id"]

        # 任务上冻结的依据可查
        r = client.get(f"/api/inspection-tasks/tasks/{task_id}")
        detail = r.json()
        assert detail["frozen_grade"] == "C"
        assert detail["rule_version_id"] is not None

        # 审计轨迹
        r = client.get(f"/api/inspection-tasks/tasks/{task_id}/logs")
        assert r.status_code == 200
        assert any(l["change_type"] == "创建" for l in r.json())

        # 风险突升 -> 高优先级
        r = client.post(f"/api/inspection-tasks/institutions/{inst.id}/risk-surge",
                        json={"reason": "群众集中举报"})
        assert r.status_code == 200
        r = client.get(f"/api/inspection-tasks/tasks/{task_id}")
        assert r.json()["priority"] == "高"

        # 延期
        new_date = (date.today() + timedelta(days=45)).isoformat()
        r = client.post(f"/api/inspection-tasks/tasks/{task_id}/postpone",
                        json={"new_date": new_date, "reason": "避开关爱日活动"})
        assert r.status_code == 200
        assert r.json()["scheduled_date"] == new_date

        # 结果回写后锁定：延期被拒 409
        r = client.post(f"/api/inspection-tasks/tasks/{task_id}/result",
                        json={"result": "未发现异常"})
        assert r.status_code == 200
        r = client.post(f"/api/inspection-tasks/tasks/{task_id}/postpone",
                        json={"new_date": new_date, "reason": "试图改写"})
        assert r.status_code == 409

        # 缺口列表可查
        r = client.get("/api/inspection-tasks/gaps")
        assert r.status_code == 200

    def test_suspend_and_rule_versions_endpoints(self, client, db_session):
        inst = _make_institution(db_session, "API2")
        _make_score(db_session, inst, ComplianceGrade.POOR, 40.0)
        client.post("/api/inspection-tasks/inspectors", json={"name": "API2-周队"})
        gen = client.post("/api/inspection-tasks/quarterly-plan",
                          json={"institution_ids": [inst.id]}).json()
        assert gen["tasks_created"] == 4

        r = client.post(f"/api/inspection-tasks/institutions/{inst.id}/suspend",
                        json={"reason": "停业整顿"})
        assert r.status_code == 200
        assert len(r.json()["cancelled_task_ids"]) == 4

        r = client.get("/api/inspection-tasks/rule-versions/active")
        assert r.status_code == 200
        assert r.json()["version_code"]

        r = client.post("/api/inspection-tasks/rule-versions", json={
            "version_code": "2026Q4-api-v3",
            "effective_from": date.today().isoformat(),
            "change_summary": "API 换版冒烟",
            "rules": {"window_days": 20},
        })
        assert r.status_code == 200
        assert r.json()["open_tasks_unchanged"] == 0  # 任务均已取消


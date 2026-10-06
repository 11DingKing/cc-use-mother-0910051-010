"""检查任务调度测试。

覆盖用户需求：
1. 计划生成固定风险评分与频率规则版本（任务留痕评分依据与规则版本）；
2. 执法人员容量约束排期，高风险机构优先，不被排到后面；
3. 延期、机构停业、风险突升、检查结果回写时计算保留/取消/重排并说明原因；
4. 已执行的任务不被新规则改写；
5. 同一机构重复任务与执法人员时间冲突在事务中解决；
6. 批量重排中不可安排的任务保留为明确缺口，不静默丢失。
"""

import itertools
from datetime import date, datetime, timedelta

import pytest

from app.models import (
    Institution, InstitutionType, InstitutionLicense,
    Practitioner, ViolationClue, ClueType, ClueStatus, CluePriority,
    ComplianceScore, InspectionTask, TaskStatus, TaskKind,
)

TODAY = date.today()
_id_card_counter = itertools.count(1)


# ---------- 测试数据构造辅助 ----------


def _make_institution(db_session, name, code, with_license=True):
    inst = Institution(
        name=name,
        unified_social_code=code,
        institution_type=InstitutionType.CLINIC,
        legal_person="测试法人",
        address="测试地址",
        phone="021-00000000",
        registration_date=date(2020, 1, 1),
        business_scope="医疗美容科；美容皮肤科",
    )
    db_session.add(inst)
    db_session.flush()
    if with_license:
        db_session.add(InstitutionLicense(
            institution_id=inst.id,
            license_number=f"LIC-{code}",
            issuing_authority="测试卫健委",
            issue_date=date(2020, 1, 15),
            valid_until=date(2030, 1, 14),
            approved_surgeries="美容皮肤科全部项目",
            is_valid=True,
        ))
        db_session.flush()
    return inst


def _add_unlicensed_practitioners(db_session, inst, count):
    for _ in range(count):
        n = next(_id_card_counter)
        db_session.add(Practitioner(
            name=f"无证人员{n}",
            id_card=f"3101019999000{n:05d}",
            gender="女",
            institution_id=inst.id,
            position="操作师",
        ))
    db_session.flush()


def _add_verified_clue(db_session, inst, clue_type):
    db_session.add(ViolationClue(
        clue_type=clue_type,
        title=f"已核实线索-{inst.name}",
        description="测试用已核实违规线索",
        institution_id=inst.id,
        source="测试",
        priority=CluePriority.HIGH,
        status=ClueStatus.VERIFIED,
        conclusion="经查实违规",
    ))
    db_session.flush()


def make_grade_a(db_session, code):
    """100分：许可证有效，无其他问题。"""
    return _make_institution(db_session, f"测试A级机构{code}", code, with_license=True)


def make_grade_b(db_session, code):
    """85分：无许可证（仅扣许可证有效性15分）。"""
    return _make_institution(db_session, f"测试B级机构{code}", code, with_license=False)


def make_grade_c(db_session, code):
    """66分：全员无证(-20) + 已核实虚假宣传(-10) + 1条已核实违规(-4)。"""
    inst = _make_institution(db_session, f"测试C级机构{code}", code, with_license=True)
    _add_unlicensed_practitioners(db_session, inst, 2)
    _add_verified_clue(db_session, inst, ClueType.FALSE_ADVERTISEMENT)
    return inst


def make_grade_d(db_session, code):
    """46分：无许可证(-15) + 全员无证(-20) + 已核实速成班(-15) + 1条已核实违规(-4)。"""
    inst = _make_institution(db_session, f"测试D级机构{code}", code, with_license=False)
    _add_unlicensed_practitioners(db_session, inst, 2)
    _add_verified_clue(db_session, inst, ClueType.QUICK_TRAINING)
    return inst


def _create_score(client, db_session, institution_id, days_ago=0):
    resp = client.post(
        f"/api/compliance-score/calculate/{institution_id}?generate_plans=false"
    )
    assert resp.status_code == 200
    data = resp.json()
    if days_ago:
        score = db_session.get(ComplianceScore, data["id"])
        score.scored_at = datetime.utcnow() - timedelta(days=days_ago)
        db_session.flush()
    return data


def _create_inspector(client, name="测试稽查员", capacity=1):
    resp = client.post(
        "/api/scheduling/inspectors",
        json={"name": name, "daily_capacity": capacity},
    )
    assert resp.status_code == 200
    return resp.json()


def _generate(client, start, end, **kwargs):
    payload = {"period_start": str(start), "period_end": str(end)}
    payload.update(kwargs)
    resp = client.post("/api/scheduling/tasks/generate", json=payload)
    assert resp.status_code == 200
    return resp.json()


def _tasks_of(client, institution_id):
    resp = client.get(f"/api/scheduling/tasks?institution_id={institution_id}")
    assert resp.status_code == 200
    return resp.json()


def _get_task(client, task_id):
    resp = client.get(f"/api/scheduling/tasks/{task_id}")
    assert resp.status_code == 200
    return resp.json()


@pytest.fixture
def active_rule_version(client):
    resp = client.post("/api/scheduling/rule-versions", json={
        "version_code": "RV-TEST-001",
        "name": "测试频率规则v1",
        "rules": {
            "A": {"frequency_months": 24, "priority": "低"},
            "B": {"frequency_months": 12, "priority": "低"},
            "C": {"frequency_months": 6, "priority": "中"},
            "D": {"frequency_months": 3, "priority": "高"},
        },
    })
    assert resp.status_code == 200
    return resp.json()


# ---------- 规则版本与执法人员管理 ----------


class TestRuleVersionAndInspector:
    def test_rule_version_lifecycle(self, client, active_rule_version):
        rv1 = active_rule_version
        assert rv1["is_active"] is True

        # 重复编码 → 400
        resp = client.post("/api/scheduling/rule-versions", json={
            "version_code": "RV-TEST-001", "name": "重复",
            "rules": {"A": {"frequency_months": 24, "priority": "低"}},
        })
        assert resp.status_code == 400

        # 非法等级键 → 400
        resp = client.post("/api/scheduling/rule-versions", json={
            "version_code": "RV-TEST-002", "name": "非法等级",
            "rules": {"E": {"frequency_months": 24, "priority": "低"}},
        })
        assert resp.status_code == 400

        # 新版本启用后旧版本自动停用
        resp = client.post("/api/scheduling/rule-versions", json={
            "version_code": "RV-TEST-003", "name": "测试频率规则v2",
            "rules": {"D": {"frequency_months": 3, "priority": "高"}},
        })
        assert resp.status_code == 200
        rv2 = resp.json()
        assert rv2["is_active"] is True

        versions = client.get("/api/scheduling/rule-versions").json()
        active = [v for v in versions if v["is_active"]]
        assert len(active) == 1 and active[0]["id"] == rv2["id"]

        # 重新启用旧版本
        resp = client.post(f"/api/scheduling/rule-versions/{rv1['id']}/activate")
        assert resp.status_code == 200
        assert resp.json()["is_active"] is True

    def test_inspector_crud(self, client):
        insp = _create_inspector(client, "测试稽查员甲", capacity=3)
        assert insp["daily_capacity"] == 3
        assert insp["active_task_count"] == 0

        resp = client.post("/api/scheduling/inspectors",
                           json={"name": "测试稽查员甲", "daily_capacity": 1})
        assert resp.status_code == 400

        resp = client.put(f"/api/scheduling/inspectors/{insp['id']}",
                          json={"daily_capacity": 5})
        assert resp.status_code == 200
        assert resp.json()["daily_capacity"] == 5

        inspectors = client.get("/api/scheduling/inspectors").json()
        assert any(i["id"] == insp["id"] for i in inspectors)


# ---------- 任务生成：固定评分依据与规则版本、容量约束、高风险优先 ----------


class TestTaskGeneration:
    def test_generation_pins_score_and_rule_version(
        self, client, db_session, active_rule_version
    ):
        inst = make_grade_d(db_session, "SCHED-PIN-D")
        score = _create_score(client, db_session, inst.id, days_ago=30)
        _create_inspector(client, capacity=2)

        result = _generate(client, TODAY, TODAY + timedelta(days=92))
        assert result["version_code"] == "RV-TEST-001"
        assert result["created_count"] == 1

        task = _tasks_of(client, inst.id)[0]
        # 固定评分依据与规则版本
        assert task["compliance_score_id"] == score["id"]
        assert task["rule_version_id"] == active_rule_version["id"]
        assert task["version_code"] == "RV-TEST-001"
        assert task["priority"] == "高"
        assert "评分#" in task["priority_reason"]
        assert task["status"] == "已排期"

    def test_new_rule_version_does_not_rewrite_existing_tasks(
        self, client, db_session, active_rule_version
    ):
        inst_d = make_grade_d(db_session, "SCHED-PIN2-D")
        _create_score(client, db_session, inst_d.id, days_ago=30)
        _create_inspector(client, capacity=5)
        _generate(client, TODAY, TODAY + timedelta(days=92))

        # 启用新规则版本后，已生成任务仍固定在原版本
        resp = client.post("/api/scheduling/rule-versions", json={
            "version_code": "RV-TEST-009", "name": "测试频率规则v2",
            "rules": {
                "A": {"frequency_months": 24, "priority": "低"},
                "B": {"frequency_months": 12, "priority": "低"},
                "C": {"frequency_months": 6, "priority": "高"},
                "D": {"frequency_months": 3, "priority": "高"},
            },
        })
        assert resp.status_code == 200
        rv2 = resp.json()

        inst_c = make_grade_c(db_session, "SCHED-PIN2-C")
        _create_score(client, db_session, inst_c.id, days_ago=150)
        _generate(client, TODAY, TODAY + timedelta(days=92))

        task_d = _tasks_of(client, inst_d.id)[0]
        task_c = _tasks_of(client, inst_c.id)[0]
        assert task_d["rule_version_id"] == active_rule_version["id"]
        assert task_c["rule_version_id"] == rv2["id"]
        assert task_c["priority"] == "高"  # 新规则中 C 对应高优先级

    def test_capacity_constraint_and_high_risk_first(
        self, client, db_session, active_rule_version
    ):
        """1名执法人员日容量1：高风险先排，容量耗尽后低风险形成明确缺口。"""
        inst_a = make_grade_a(db_session, "SCHED-CAP-A")
        inst_b = make_grade_b(db_session, "SCHED-CAP-B")
        inst_c = make_grade_c(db_session, "SCHED-CAP-C")
        inst_d = make_grade_d(db_session, "SCHED-CAP-D")
        _create_score(client, db_session, inst_a.id, days_ago=719)   # 应检 today+1
        _create_score(client, db_session, inst_b.id, days_ago=360)   # 应检 today
        _create_score(client, db_session, inst_c.id, days_ago=180)   # 应检 today
        _create_score(client, db_session, inst_d.id, days_ago=90)    # 应检 today
        _create_inspector(client, capacity=1)

        result = _generate(client, TODAY, TODAY + timedelta(days=2))
        assert result["created_count"] == 3
        assert result["gap_count"] == 1

        task_d = _tasks_of(client, inst_d.id)[0]
        task_c = _tasks_of(client, inst_c.id)[0]
        task_b = _tasks_of(client, inst_b.id)[0]
        task_a = _tasks_of(client, inst_a.id)[0]

        # 高优先级（高风险机构）占用最早档期
        assert task_d["scheduled_date"] == str(TODAY)
        assert task_c["scheduled_date"] == str(TODAY + timedelta(days=1))
        assert task_b["scheduled_date"] == str(TODAY + timedelta(days=2))
        # 容量耗尽：A级任务保留为明确缺口而非静默丢失
        assert task_a["status"] == "待排期"
        assert task_a["scheduled_date"] is None
        assert "容量不足" in task_a["unschedulable_reason"]

        gaps = client.get("/api/scheduling/tasks/gaps").json()
        assert [g["id"] for g in gaps] == [task_a["id"]]

    def test_high_risk_not_pushed_behind(
        self, client, db_session, active_rule_version
    ):
        """即使低风险机构应检日期更早，高风险机构仍先占用档期。"""
        inst_a = make_grade_a(db_session, "SCHED-ORD-A")
        inst_d = make_grade_d(db_session, "SCHED-ORD-D")
        _create_score(client, db_session, inst_a.id, days_ago=720)  # 应检 today
        _create_score(client, db_session, inst_d.id, days_ago=89)   # 应检 today+1
        _create_inspector(client, capacity=1)

        _generate(client, TODAY, TODAY + timedelta(days=1))
        task_a = _tasks_of(client, inst_a.id)[0]
        task_d = _tasks_of(client, inst_d.id)[0]
        assert task_d["scheduled_date"] == str(TODAY)
        assert task_a["scheduled_date"] == str(TODAY + timedelta(days=1))

    def test_load_balancing_across_inspectors(
        self, client, db_session, active_rule_version
    ):
        inst1 = make_grade_d(db_session, "SCHED-BAL-1")
        inst2 = make_grade_d(db_session, "SCHED-BAL-2")
        _create_score(client, db_session, inst1.id, days_ago=90)   # 应检 today
        _create_score(client, db_session, inst2.id, days_ago=90)   # 应检 today
        _create_inspector(client, "均衡稽查员甲", capacity=1)
        _create_inspector(client, "均衡稽查员乙", capacity=1)

        _generate(client, TODAY, TODAY + timedelta(days=2))
        t1 = _tasks_of(client, inst1.id)[0]
        t2 = _tasks_of(client, inst2.id)[0]
        # 同日容量约束下分摊到不同执法人员
        assert t1["scheduled_date"] == str(TODAY)
        assert t2["scheduled_date"] == str(TODAY)
        assert t1["inspector_id"] != t2["inspector_id"]

    def test_no_active_inspector_means_explicit_gap(
        self, client, db_session, active_rule_version
    ):
        inst = make_grade_d(db_session, "SCHED-NOINSP-D")
        _create_score(client, db_session, inst.id, days_ago=30)
        insp = _create_inspector(client, capacity=1)
        client.put(f"/api/scheduling/inspectors/{insp['id']}",
                   json={"is_active": False})

        result = _generate(client, TODAY, TODAY + timedelta(days=92))
        assert result["gap_count"] == 1
        task = _tasks_of(client, inst.id)[0]
        assert task["status"] == "待排期"
        assert "无可用执法人员" in task["unschedulable_reason"]

    def test_generate_dedup_idempotent(
        self, client, db_session, active_rule_version
    ):
        """同一机构同一到期季度重复生成时去重，不产生重复任务。"""
        inst = make_grade_d(db_session, "SCHED-DEDUP-D")
        _create_score(client, db_session, inst.id, days_ago=30)
        _create_inspector(client, capacity=2)

        first = _generate(client, TODAY, TODAY + timedelta(days=92))
        assert first["created_count"] == 1

        second = _generate(client, TODAY, TODAY + timedelta(days=92))
        assert second["created_count"] == 0
        kept = [d for d in second["decisions"] if d["action"] == "保留"]
        assert len(kept) == 1
        assert "不重复生成" in kept[0]["reason"]

        assert len(_tasks_of(client, inst.id)) == 1

    def test_generate_with_invalid_rule_version_rolls_back(
        self, client, db_session, active_rule_version
    ):
        inst = make_grade_d(db_session, "SCHED-ATOMIC-D")
        _create_score(client, db_session, inst.id, days_ago=30)
        _create_inspector(client, capacity=1)

        resp = client.post("/api/scheduling/tasks/generate", json={
            "period_start": str(TODAY),
            "period_end": str(TODAY + timedelta(days=30)),
            "rule_version_id": 99999,
        })
        assert resp.status_code == 404
        assert _tasks_of(client, inst.id) == []


# ---------- 触发器：延期 ----------


class TestDelayTrigger:
    def test_delay_to_date_resolves_conflicts(
        self, client, db_session, active_rule_version
    ):
        """延期到指定日期后，时间冲突在事务内解决，并留痕说明。"""
        inst_d = make_grade_d(db_session, "SCHED-DLY-D")
        inst_c = make_grade_c(db_session, "SCHED-DLY-C")
        _create_score(client, db_session, inst_d.id, days_ago=90)
        _create_score(client, db_session, inst_c.id, days_ago=180)
        insp = _create_inspector(client, capacity=1)
        _generate(client, TODAY, TODAY + timedelta(days=4))

        task_d = _tasks_of(client, inst_d.id)[0]
        task_c = _tasks_of(client, inst_c.id)[0]
        assert task_d["scheduled_date"] == str(TODAY)
        assert task_c["scheduled_date"] == str(TODAY + timedelta(days=1))

        # D 延期到 C 的档期：两者冲突，重排后各占一天
        resp = client.post(f"/api/scheduling/tasks/{task_d['id']}/delay", json={
            "new_date": str(TODAY + timedelta(days=1)),
            "reason": "执法人员临时外出",
        })
        assert resp.status_code == 200
        result = resp.json()
        assert result["trigger"] == "延期"
        assert result["rescheduled_count"] >= 1

        task_d = _get_task(client, task_d["id"])
        task_c = _get_task(client, task_c["id"])
        assert task_d["scheduled_date"] == str(TODAY + timedelta(days=1))
        assert task_c["scheduled_date"] == str(TODAY)
        # 无时间冲突：同一执法人员同一天不超过容量1
        assert not (
            task_d["scheduled_date"] == task_c["scheduled_date"]
            and task_d["inspector_id"] == task_c["inspector_id"] == insp["id"]
        )
        # 留痕说明延期原因
        actions = [(e["action"], e["reason"] or "") for e in task_d["events"]]
        assert any(a == "重排" and "执法人员临时外出" in r for a, r in actions)

    def test_delay_without_date_finds_next_slot(
        self, client, db_session, active_rule_version
    ):
        inst_d = make_grade_d(db_session, "SCHED-DLY2-D")
        inst_c = make_grade_c(db_session, "SCHED-DLY2-C")
        _create_score(client, db_session, inst_d.id, days_ago=90)
        _create_score(client, db_session, inst_c.id, days_ago=180)
        _create_inspector(client, capacity=1)
        _generate(client, TODAY, TODAY + timedelta(days=2))

        task_c = _tasks_of(client, inst_c.id)[0]
        assert task_c["scheduled_date"] == str(TODAY + timedelta(days=1))

        resp = client.post(f"/api/scheduling/tasks/{task_c['id']}/delay", json={})
        assert resp.status_code == 200
        task_c = _get_task(client, task_c["id"])
        assert task_c["scheduled_date"] == str(TODAY + timedelta(days=2))

        task_d = _tasks_of(client, inst_d.id)[0]
        assert task_d["scheduled_date"] == str(TODAY)  # 高优先级任务不受影响

    def test_delay_frozen_task_rejected(
        self, client, db_session, active_rule_version
    ):
        inst = make_grade_d(db_session, "SCHED-DLY3-D")
        _create_score(client, db_session, inst.id, days_ago=30)
        _create_inspector(client, capacity=1)
        _generate(client, TODAY, TODAY + timedelta(days=92))
        task = _tasks_of(client, inst.id)[0]

        client.post(f"/api/scheduling/tasks/{task['id']}/result",
                    json={"result": "检查合格", "passed": True})
        resp = client.post(f"/api/scheduling/tasks/{task['id']}/delay",
                           json={"new_date": str(TODAY + timedelta(days=3))})
        assert resp.status_code == 409
        # 已执行任务未被改写
        task_after = _get_task(client, task["id"])
        assert task_after["status"] == "已完成"
        assert task_after["result"] == "检查合格"


# ---------- 触发器：机构停业 ----------


class TestClosureTrigger:
    def test_closure_cancels_active_but_not_executed(
        self, client, db_session, active_rule_version
    ):
        inst = make_grade_d(db_session, "SCHED-CLS-D")
        _create_score(client, db_session, inst.id, days_ago=30)
        _create_inspector(client, capacity=2)
        _generate(client, TODAY, TODAY + timedelta(days=92))

        task = _tasks_of(client, inst.id)[0]
        # 先执行完毕（结果不合格 → 自动生成复查任务）
        resp = client.post(f"/api/scheduling/tasks/{task['id']}/result",
                           json={"result": "发现无证上岗", "passed": False})
        assert resp.status_code == 200
        follow_ups = [t for t in _tasks_of(client, inst.id)
                      if t["task_kind"] == "复查"]
        assert len(follow_ups) == 1

        # 机构停业：活动任务取消，已完成任务冻结
        resp = client.post(
            f"/api/scheduling/institutions/{inst.id}/closure",
            json={"closed": True, "reason": "机构申请停业整顿"},
        )
        assert resp.status_code == 200
        result = resp.json()
        assert result["trigger"] == "机构停业"
        assert result["cancelled_count"] == 1
        cancel = [d for d in result["decisions"] if d["action"] == "取消"][0]
        assert "机构停业" in cancel["reason"]

        completed = _get_task(client, task["id"])
        assert completed["status"] == "已完成"
        assert completed["result"] == "发现无证上岗"

        follow_up = _get_task(client, follow_ups[0]["id"])
        assert follow_up["status"] == "已取消"

        inst_info = client.get(f"/api/institutions/{inst.id}").json()
        assert inst_info["operating_status"] == "停业"

        # 停业机构不再生成新任务
        gen = _generate(client, TODAY, TODAY + timedelta(days=92))
        skipped = [d for d in gen["decisions"]
                   if d["institution_id"] == inst.id]
        assert skipped and skipped[0]["action"] == "跳过"
        assert "停业" in skipped[0]["reason"]

        # 恢复营业
        resp = client.post(
            f"/api/scheduling/institutions/{inst.id}/closure",
            json={"closed": False},
        )
        assert resp.status_code == 200
        inst_info = client.get(f"/api/institutions/{inst.id}").json()
        assert inst_info["operating_status"] == "正常营业"

    def test_closure_releases_capacity_to_gaps(
        self, client, db_session, active_rule_version
    ):
        """停业取消任务释放容量后，其他机构的待排期缺口被补入。"""
        inst_d = make_grade_d(db_session, "SCHED-CLS2-D")
        inst_c = make_grade_c(db_session, "SCHED-CLS2-C")
        _create_score(client, db_session, inst_d.id, days_ago=90)
        _create_score(client, db_session, inst_c.id, days_ago=180)
        _create_inspector(client, capacity=1)
        # 窗口仅1天：D 排上，C 形成缺口
        _generate(client, TODAY, TODAY)
        assert _tasks_of(client, inst_c.id)[0]["status"] == "待排期"

        resp = client.post(
            f"/api/scheduling/institutions/{inst_d.id}/closure",
            json={"closed": True, "window_start": str(TODAY),
                  "window_end": str(TODAY)},
        )
        assert resp.status_code == 200
        task_c = _tasks_of(client, inst_c.id)[0]
        assert task_c["status"] == "已排期"
        assert task_c["scheduled_date"] == str(TODAY)


# ---------- 触发器：风险突升 ----------


class TestRiskSurgeTrigger:
    def test_risk_surge_reprioritizes_and_explains(
        self, client, db_session, active_rule_version
    ):
        inst_x = make_grade_b(db_session, "SCHED-RS-X")   # 85分 B级
        inst_y = make_grade_c(db_session, "SCHED-RS-Y")   # 66分 C级
        _create_score(client, db_session, inst_x.id, days_ago=360)
        _create_score(client, db_session, inst_y.id, days_ago=180)
        _create_inspector(client, capacity=1)
        _generate(client, TODAY, TODAY + timedelta(days=4))

        task_x = _tasks_of(client, inst_x.id)[0]
        task_y = _tasks_of(client, inst_y.id)[0]
        assert task_x["priority"] == "低"
        assert task_y["scheduled_date"] == str(TODAY)
        assert task_x["scheduled_date"] == str(TODAY + timedelta(days=1))

        # X 风险突升：新增无证人员与已核实速成班线索 → 重评为 D 级
        _add_unlicensed_practitioners(db_session, inst_x, 2)
        _add_verified_clue(db_session, inst_x, ClueType.QUICK_TRAINING)
        resp = client.post(
            f"/api/scheduling/institutions/{inst_x.id}/risk-surge",
            json={"reason": "专项整治中发现新问题"},
        )
        assert resp.status_code == 200
        result = resp.json()
        assert result["trigger"] == "风险突升"
        changed = [d for d in result["decisions"] if d["action"] == "优先级调整"]
        assert len(changed) == 1
        assert changed[0]["old_priority"] == "低"
        assert changed[0]["new_priority"] == "高"
        assert "D" in changed[0]["reason"]

        task_x = _get_task(client, task_x["id"])
        task_y = _get_task(client, task_y["id"])
        assert task_x["priority"] == "高"
        assert "优先级由「低」调整为「高」" in task_x["priority_reason"]
        # 高风险后排期提前，不再排在低风险机构后面
        assert task_x["scheduled_date"] == str(TODAY)
        assert task_y["scheduled_date"] == str(TODAY + timedelta(days=1))
        # 留痕包含新旧评分依据
        surge_events = [e for e in task_x["events"]
                        if e["trigger"] == "风险突升" and e["action"] == "优先级调整"]
        assert len(surge_events) == 1
        assert surge_events[0]["new_score_id"] is not None

    def test_executed_task_frozen_under_new_rules(
        self, client, db_session, active_rule_version
    ):
        """已执行的任务不被新评分/新规则改写；新评分只影响活动任务与新任务。"""
        inst = make_grade_b(db_session, "SCHED-FRZ-X")
        old_score = _create_score(client, db_session, inst.id, days_ago=360)
        _create_inspector(client, capacity=2)
        _generate(client, TODAY, TODAY + timedelta(days=92))
        task = _tasks_of(client, inst.id)[0]

        resp = client.post(f"/api/scheduling/tasks/{task['id']}/result",
                           json={"result": "检查合格", "passed": True})
        assert resp.status_code == 200

        # 把已完成任务的应检日期推到与后续新任务不同的季度，保证新任务可生成
        completed = db_session.get(InspectionTask, task["id"])
        completed.due_date = TODAY + timedelta(days=200)
        db_session.flush()

        _add_unlicensed_practitioners(db_session, inst, 2)
        _add_verified_clue(db_session, inst, ClueType.QUICK_TRAINING)
        resp = client.post(
            f"/api/scheduling/institutions/{inst.id}/risk-surge", json={}
        )
        assert resp.status_code == 200

        frozen = _get_task(client, task["id"])
        assert frozen["status"] == "已完成"
        assert frozen["priority"] == "低"                      # 优先级未被改写
        assert frozen["compliance_score_id"] == old_score["id"]  # 评分依据未被改写
        assert frozen["due_date"] == str(TODAY + timedelta(days=200))
        assert frozen["result"] == "检查合格"

        # 本季度无活动常规任务 → 按新评分生成高优先级新任务
        new_tasks = [t for t in _tasks_of(client, inst.id)
                     if t["id"] != task["id"]]
        assert len(new_tasks) == 1
        assert new_tasks[0]["priority"] == "高"
        assert new_tasks[0]["compliance_score_id"] != old_score["id"]
        assert new_tasks[0]["status"] == "已排期"

    def test_risk_surge_with_invalid_score_rolls_back(
        self, client, db_session, active_rule_version
    ):
        inst = make_grade_d(db_session, "SCHED-RS404-D")
        _create_score(client, db_session, inst.id, days_ago=30)
        _create_inspector(client, capacity=1)
        _generate(client, TODAY, TODAY + timedelta(days=92))
        before = _tasks_of(client, inst.id)

        resp = client.post(
            f"/api/scheduling/institutions/{inst.id}/risk-surge",
            json={"new_score_id": 99999},
        )
        assert resp.status_code == 404
        # 事务回滚：任务状态完全不变
        assert _tasks_of(client, inst.id) == before


# ---------- 触发器：检查结果回写 ----------


class TestResultWriteback:
    def test_failed_result_creates_follow_up_task(
        self, client, db_session, active_rule_version
    ):
        inst = make_grade_d(db_session, "SCHED-WB-D")
        _create_score(client, db_session, inst.id, days_ago=30)
        _create_inspector(client, capacity=2)
        _generate(client, TODAY, TODAY + timedelta(days=92))
        task = _tasks_of(client, inst.id)[0]

        resp = client.post(f"/api/scheduling/tasks/{task['id']}/result", json={
            "result": "现场发现无证人员上岗", "passed": False,
        })
        assert resp.status_code == 200
        result = resp.json()
        assert result["trigger"] == "结果回写"
        actions = [d["action"] for d in result["decisions"]]
        assert "完成" in actions
        assert "新建" in actions

        done = _get_task(client, task["id"])
        assert done["status"] == "已完成"
        assert done["result"] == "现场发现无证人员上岗"
        assert done["actual_date"] == str(TODAY)

        follow_ups = [t for t in _tasks_of(client, inst.id)
                      if t["task_kind"] == "复查"]
        assert len(follow_ups) == 1
        assert follow_ups[0]["priority"] == "高"
        assert follow_ups[0]["status"] == "已排期"
        assert "不合格" in follow_ups[0]["priority_reason"]

    def test_passed_result_creates_no_follow_up(
        self, client, db_session, active_rule_version
    ):
        inst = make_grade_d(db_session, "SCHED-WB2-D")
        _create_score(client, db_session, inst.id, days_ago=30)
        _create_inspector(client, capacity=2)
        _generate(client, TODAY, TODAY + timedelta(days=92))
        task = _tasks_of(client, inst.id)[0]

        resp = client.post(f"/api/scheduling/tasks/{task['id']}/result",
                           json={"result": "检查合格", "passed": True})
        assert resp.status_code == 200
        assert len(_tasks_of(client, inst.id)) == 1

    def test_writeback_rejected_for_invalid_states(
        self, client, db_session, active_rule_version
    ):
        inst = make_grade_d(db_session, "SCHED-WB3-D")
        _create_score(client, db_session, inst.id, days_ago=30)
        insp = _create_inspector(client, capacity=1)
        # 停用执法人员 → 任务无法排期，处于待排期状态
        client.put(f"/api/scheduling/inspectors/{insp['id']}",
                   json={"is_active": False})
        _generate(client, TODAY, TODAY + timedelta(days=92))
        task = _tasks_of(client, inst.id)[0]
        assert task["status"] == "待排期"

        # 待排期任务不能回写结果
        resp = client.post(f"/api/scheduling/tasks/{task['id']}/result",
                           json={"result": "x", "passed": True})
        assert resp.status_code == 409

        # 已取消任务不能回写结果
        client.post(f"/api/scheduling/institutions/{inst.id}/closure",
                    json={"closed": True})
        resp = client.post(f"/api/scheduling/tasks/{task['id']}/result",
                           json={"result": "x", "passed": True})
        assert resp.status_code == 409


# ---------- 批量重排：冲突解决与明确缺口 ----------


class TestBatchReplan:
    def test_replan_resolves_conflicts_and_keeps_gap(
        self, client, db_session, active_rule_version
    ):
        """人工制造同日冲突后批量重排：高优先级保留档期，其余成为明确缺口。"""
        inst_d = make_grade_d(db_session, "SCHED-RP-D")
        inst_c = make_grade_c(db_session, "SCHED-RP-C")
        _create_score(client, db_session, inst_d.id, days_ago=90)
        _create_score(client, db_session, inst_c.id, days_ago=180)
        insp = _create_inspector(client, capacity=1)
        _generate(client, TODAY, TODAY + timedelta(days=4))

        task_d = _tasks_of(client, inst_d.id)[0]
        task_c = _tasks_of(client, inst_c.id)[0]
        # 人工制造冲突：两个任务挤在同一天同一执法人员（容量1）
        conflict = db_session.get(InspectionTask, task_c["id"])
        conflict.scheduled_date = TODAY
        conflict.inspector_id = insp["id"]
        db_session.flush()

        resp = client.post("/api/scheduling/replan", json={
            "window_start": str(TODAY),
            "window_end": str(TODAY),
            "reason": "专项整治集中重排",
        })
        assert resp.status_code == 200
        result = resp.json()
        assert result["trigger"] == "批量重排"
        assert result["kept_count"] == 1
        assert result["gap_count"] == 1

        task_d = _get_task(client, task_d["id"])
        task_c = _get_task(client, task_c["id"])
        # 高优先级保留档期
        assert task_d["status"] == "已排期"
        assert task_d["scheduled_date"] == str(TODAY)
        # 低优先级成为明确缺口：任务仍在、原因明确、未静默丢失
        assert task_c["status"] == "待排期"
        assert task_c["scheduled_date"] is None
        assert "容量不足" in task_c["unschedulable_reason"]
        gap_decision = [d for d in result["decisions"] if d["action"] == "缺口"]
        assert len(gap_decision) == 1
        assert "专项整治集中重排" in gap_decision[0]["reason"]

        gaps = client.get("/api/scheduling/tasks/gaps").json()
        assert any(g["id"] == task_c["id"] for g in gaps)

        # 扩大窗口后重排，缺口被补入
        resp = client.post("/api/scheduling/replan", json={
            "window_start": str(TODAY),
            "window_end": str(TODAY + timedelta(days=3)),
        })
        assert resp.status_code == 200
        task_c = _get_task(client, task_c["id"])
        assert task_c["status"] == "已排期"
        assert task_c["unschedulable_reason"] is None

    def test_replan_cancels_duplicate_tasks(
        self, client, db_session, active_rule_version
    ):
        """同一机构的重复活动任务在事务中被取消，只保留一个。"""
        inst = make_grade_d(db_session, "SCHED-DUP-D")
        score = _create_score(client, db_session, inst.id, days_ago=30)
        _create_inspector(client, capacity=5)
        _generate(client, TODAY, TODAY + timedelta(days=92))
        original = _tasks_of(client, inst.id)[0]

        # 人工制造同机构同季度的重复任务（模拟历史脏数据）
        dup = InspectionTask(
            task_no="JC-DUP-001",
            institution_id=inst.id,
            compliance_score_id=score["id"],
            rule_version_id=original["rule_version_id"],
            task_kind=TaskKind.REGULAR,
            round_no=2,
            priority=CluePriority.LOW,
            due_date=date.fromisoformat(original["due_date"]),
            scheduled_date=TODAY + timedelta(days=1),
            status=TaskStatus.SCHEDULED,
        )
        db_session.add(dup)
        db_session.flush()

        resp = client.post("/api/scheduling/replan", json={
            "window_start": str(TODAY),
            "window_end": str(TODAY + timedelta(days=92)),
        })
        assert resp.status_code == 200
        result = resp.json()
        assert result["cancelled_count"] == 1
        cancel = [d for d in result["decisions"] if d["action"] == "取消"][0]
        assert "重复检查任务" in cancel["reason"]
        assert original["task_no"] in cancel["reason"]

        tasks = _tasks_of(client, inst.id)
        active = [t for t in tasks if t["status"] in ("待排期", "已排期")]
        cancelled = [t for t in tasks if t["status"] == "已取消"]
        assert len(active) == 1
        assert active[0]["id"] == original["id"]  # 高优先级者保留
        assert len(cancelled) == 1

    def test_replan_does_not_touch_executed_tasks(
        self, client, db_session, active_rule_version
    ):
        inst = make_grade_d(db_session, "SCHED-RP2-D")
        _create_score(client, db_session, inst.id, days_ago=30)
        _create_inspector(client, capacity=1)
        _generate(client, TODAY, TODAY + timedelta(days=92))
        task = _tasks_of(client, inst.id)[0]
        client.post(f"/api/scheduling/tasks/{task['id']}/result",
                    json={"result": "检查合格", "passed": True})

        resp = client.post("/api/scheduling/replan", json={
            "window_start": str(TODAY),
            "window_end": str(TODAY + timedelta(days=92)),
        })
        assert resp.status_code == 200
        done = _get_task(client, task["id"])
        assert done["status"] == "已完成"
        assert done["scheduled_date"] == str(TODAY)
        # 已完成任务不产生任何重排/取消留痕
        mutated = [e for e in done["events"]
                   if e["action"] in ("重排", "取消", "优先级调整")]
        assert mutated == []


# ---------- 留痕审计 ----------


class TestEventAudit:
    def test_events_queryable_by_task_and_trigger(
        self, client, db_session, active_rule_version
    ):
        inst = make_grade_d(db_session, "SCHED-EV-D")
        _create_score(client, db_session, inst.id, days_ago=30)
        _create_inspector(client, capacity=1)
        _generate(client, TODAY, TODAY + timedelta(days=92))
        task = _tasks_of(client, inst.id)[0]

        client.post(f"/api/scheduling/tasks/{task['id']}/delay",
                    json={"new_date": str(TODAY + timedelta(days=2))})

        events = client.get(
            f"/api/scheduling/events?task_id={task['id']}"
        ).json()
        triggers = {e["trigger"] for e in events}
        assert "计划生成" in triggers
        assert "延期" in triggers

        delay_events = client.get(
            "/api/scheduling/events?trigger=延期"
        ).json()
        assert any(e["task_id"] == task["id"] for e in delay_events)

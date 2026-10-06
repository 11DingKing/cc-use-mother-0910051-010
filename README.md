# 医疗美容执业与项目合规服务

本项目是使用 Python、FastAPI 与 SQLite 实现的服务端应用，覆盖机构、人员资质、项目分级、执业范围、合规线索和监管处置。它可在单个 Linux 应用容器内完成安装、测试、编译和接口验收，不依赖浏览器、外部数据库、缓存、消息队列或额外运行服务。

## 检查任务调度（/api/scheduling）

面向专项整治的季度检查计划调度，解决"风险等级变化后高风险机构反而被排到后面"的问题：

- **计划生成固定依据**：任务生成时固定 `compliance_score_id`（评分依据）与 `rule_version_id`（频率规则版本），规则升级不改写已生成任务的依据；频率规则按版本管理（A/B/C/D 等级 → 检查频率/优先级）。
- **容量约束排期**：执法人员按日容量接单，任务按（优先级、应检日期）排序占用档期，高风险机构优先排期。
- **四类触发器重排**：延期（`/tasks/{id}/delay`）、机构停业（`/institutions/{id}/closure`）、风险突升（`/institutions/{id}/risk-surge`）、检查结果回写（`/tasks/{id}/result`，不合格自动生成复查任务），以及批量重排（`/replan`）。每次触发都在单个事务内计算需要保留、取消或重排的任务，逐条说明优先级与排期变化原因（`/events` 留痕可查）。
- **已执行冻结**：进行中/已完成/已取消的任务不参与任何重排，新规则不会改写。
- **事务一致性**：同一机构同一到期季度的重复任务、执法人员同日超容量的时间冲突，均在事务内解决（保存点 + 提交前一致性校验，失败整体回滚）。
- **明确缺口**：容量不足时任务保留为"待排期"缺口并记录原因（`/tasks/gaps`），容量释放后可补入，绝不静默丢失。

## 安装

```bash
python3 -m pip install -r requirements.txt -r requirements-dev.txt
```

## 测试

```bash
python3 seed_data.py && python3 -m pytest -q
```

## 编译

```bash
python3 -m compileall -q .
```

## 接口验收

```bash
python3 -c "from app.main import app; assert len(app.routes) > 5; print(len(app.routes))"
```

## 启动

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

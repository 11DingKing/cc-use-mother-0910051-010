# 医疗美容执业与项目合规服务

本项目是使用 Python、FastAPI 与 SQLite 实现的服务端应用，覆盖机构、人员资质、项目分级、执业范围、合规线索和监管处置。它可在单个 Linux 应用容器内完成安装、测试、编译和接口验收，不依赖浏览器、外部数据库、缓存、消息队列或额外运行服务。

## 容量约束检查任务（专项整治）

专项整治期间机构风险等级不断变化，检查计划围绕以下原则构建：

- **依据版本化冻结**：评分项满分、等级分数线、等级→检查频率/年度轮次/优先级映射以
  `RuleVersion`（`rule_versions.rules_snapshot`）不可变快照管理；每次评分
  （`compliance_scores.rule_version_id`）与每个检查任务（`inspection_tasks` 上的
  `frozen_score/frozen_grade/frozen_frequency/priority/rule_version_id`）都固化生成时
  采用的依据。换版只影响之后生成的任务，绝不改写历史任务。
- **有容量约束的检查任务**：季度计划生成时，按「优先级（高>中>低）→风险评分（低分为高风险）
  →最早应检日期」排序，占用执法人员（`inspectors.daily_capacity`）的每日容量并避开请假
  冻结（`inspector_blocks`）。排不进窗口的任务保留为 `无法排期` 并显式登记排期缺口
  （`schedule_gaps`），不静默丢失。
- **事件驱动重算**：检查延期、机构停业/复业、风险突升、检查结果回写、规则换版、批量重排
  均在**单个数据库事务**内计算每个任务应保留、取消还是重排，全部决策写入审计轨迹
  （`task_change_logs`，含旧/新状态、优先级、日期、人员、触发事件与原因）。
- **不可变的已执行任务**：进行中/已完成任务（`IN_PROGRESS/COMPLETED`）锁定，任何重排、
  延期或新规则都不能改写，违规操作返回 409；重复回写同样被拒绝。
- **重复与冲突防护**：同一机构同季度同轮次的未终结任务在应用层拒绝，并由数据库部分唯一
  索引 `uq_institution_open_task` 兜底；同一执法人员同日任务数不超过日容量。
- **批量重排保留缺口**：逐个任务独立竞争容量，个别任务无法安排时以缺口挂账，不影响其他
  任务；增援或冲突解除后再次重排，缺口显式消除。

### 主要接口（前缀 `/api/inspection-tasks`）

| 方法与路径 | 说明 |
| --- | --- |
| `POST /inspectors`、`GET /inspectors`、`PATCH /inspectors/{id}` | 执法人员与日容量 |
| `POST /inspectors/{id}/blocks` | 执法人员请假/不可用日期 |
| `GET /rule-versions`、`POST /rule-versions` | 规则版本查询与换版 |
| `POST /quarterly-plan` | 生成容量约束的季度检查任务 |
| `GET /tasks`、`GET /tasks/{id}`、`GET /tasks/{id}/logs` | 任务与审计轨迹查询 |
| `GET /gaps` | 排期缺口（默认只看未解决） |
| `POST /tasks/{id}/start` | 开始执行（进入锁定的进行中状态） |
| `POST /tasks/{id}/postpone` | 检查延期并在事务内重排 |
| `POST /tasks/{id}/result` | 检查结果回写（终结并锁定，联动重排） |
| `POST /institutions/{id}/risk-surge` | 风险突升：提级并尝试提前排期 |
| `POST /institutions/{id}/suspend`、`/resume` | 机构停业（取消未执行任务）/复业 |
| `POST /batch-reschedule` | 批量重排，区分保留/重排/缺口/锁定 |

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

# 博物馆藏品来源与返还审查

标准库实现、SQLite 持久化的独立项目。它管理藏品、历史流转事件、来源引用、证据、权利主张和审查阶段，并提供面向公众、主张人、审查员和工作人员的分层视图。

## 运行

```bash
python3 app.py --init --seed
python3 app.py
```

访问 <http://127.0.0.1:8103>。数据库默认是 `provenance.db`。测试命令：

```bash
python3 -m unittest -v
```

演示身份通过 `X-User-Id` 传入：`staff`、`reviewer1`、`claimant1`、`public`。

## 主要接口

- `POST /api/objects`、`GET /api/objects`、`GET /api/objects/{id}`：藏品登记与分层查看。
- `POST /api/objects/{id}/update`：更新藏品并创建完整快照。
- `POST /api/sources`、`POST /api/objects/{id}/events`：来源与流转事件。
- `POST /api/objects/{id}/evidence`：上传证据，服务端计算 SHA-256。
- `POST /api/objects/{id}/claims`：提交权利主张。
- `POST /api/claims/{id}/transition`：按 `submitted → under_review → negotiating → resolved_return/rejected` 流转。
- `GET /api/objects/{id}/history` 与 `/history/{version}`：版本历史及历史快照。
- `POST /api/objects/{id}/loans`（工作人员）：提交临时外借申请，含借展时段与用途。
- `POST /api/loans/{id}/approve` / `reject`（审查员）、`POST /api/loans/{id}/return`（工作人员/审查员）、`GET /api/loans` 与 `GET /api/objects/{id}/loans`。

公众看不到持有人和内部事件；主张人只能查看自己的主张；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。

## 临时外借审批规则

- 工作人员提交申请（藏品、借展起止日期、用途），审查员审批；申请列表会实时给出阻塞项。
- 通过条件（任一不满足即阻塞，接口返回全部阻塞原因 `loan_blocked`）：
  1. 藏品没有未结权利主张（`submitted/under_review/negotiating`）；
  2. 每个公开流转事件（`visibility=public`）都登记了有效来源（内部事件不要求来源）；
  3. 藏品至少有一条内部证据（公开证据不计）；
  4. 借展时段不与同一藏品其他 `approved` 外借重叠（归还留档后不再占用时段；首尾相接不算重叠）。
- 通过时保存批准基线（藏品字段、事件/证据/主张 ID）。批准后若新增主张、新增事件、新增证据或修改藏品资料，批准自动转为 `invalidated` 并逐项显示冲突；失效后不能归还，需重新申请审批。
- 工作人员/审查员确认归还后状态为 `returned`，归还时间、经手人、说明与批准信息写入 `archive` 留档。
- 外借数据仅工作人员和审查员可见；主张人与公众无权访问。

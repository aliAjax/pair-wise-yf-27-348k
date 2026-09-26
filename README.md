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
- `POST /api/loans`、`GET /api/loans`、`GET /api/loans/{id}`：工作人员提交临时外借申请（藏品、借展时段、用途），列表与详情展示阻塞原因和冲突项。
- `POST /api/loans/{id}/review`：审查员批准或驳回。批准需同时满足：无未结主张、每个公开流转事件都有来源、至少一条内部证据、同时段无其他已批准外借；否则返回 409 及全部阻塞项。
- `POST /api/loans/{id}/return`：工作人员登记归还，记录留档（状态、经手人、时间、备注均保留）。

外借批准生效后，若该藏品新增主张、事件、证据或资料被修改，批准立即失效并记录冲突项；同一藏品的已批准外借时段不可重叠。

公众看不到持有人和内部事件；主张人只能查看自己的主张；阶段不能跳跃或从终态重新打开；每次对象变化都会保存 JSON 快照和审计记录。

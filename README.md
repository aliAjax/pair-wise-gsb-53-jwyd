# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、法定天数、补件期限和材料完整性和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情。
- `GET /api/records/{id}/audit`：审计时间线。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 主办与协办

收案时必须在`data`中指定一名主办`primary_agent`，可另指定协办列表`co_agents`（不得与主办重复）。经办人信息保存在记录`payload`中：`primary_agent`、`co_agents`、`pending_transfer`（待接收的转交）、`former_agents`（历任主办）。

在角色权限之上，动作还按经办人校验：

- `submit`/`respond`（提交与补交材料）：主办或协办。
- `decide`/`close`/`appeal`（决定、结案、上诉）：仅主办。
- `request_evidence`：官方法务动作，不受经办人限制。
- 记录详情与时间线：主办、协办、待接收的新主办、历任主办可查看；`admin`、`intake_officer`、`case_officer`、`supervisor`岗位角色不受限。

### 转交流程

- `transfer`：主办本人（或`supervisor`/`admin`，用于律师离职等情形）发起，`data`为`{"to_user":"...","note":"..."}`。生成待接收转交，接收前原主办照常办理。已结案或有待接收转交的案件不能再次发起。
- `accept_transfer`：仅被指派的`to_user`可执行。接收后主办身份立即切换，旧主办进入`former_agents`保留历史查看；新主办如原为协办则自动移出协办列表。
- `decline_transfer`：仅被指派的`to_user`可执行。拒绝后转交作废，案件归还原主办。

收案指派、转交发起、接收与拒绝都会写入审计时间线（`created`、`transfer`、`accept_transfer`、`decline_transfer`）。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。

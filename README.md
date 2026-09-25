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

## 主办与协办分工

收案（创建记录）时必须指定一名主办`lead_rep`，可另指定协办列表`co_reps`（默认为空，主办不能同时担任协办）。

- 协办可补交材料（`respond`）并查看时间线；`submit`、`appeal`、`decide`、`close`等动作仅主办可办理，决定与结案始终由主办负责。
- `request_evidence`为官方动作，不受分工限制；`admin`角色不受分工限制。
- 分工调整动作为`assign_co`（更新协办）、`transfer`（转交）、`accept`（接收）、`decline`（拒绝），不改变案件状态，每次指派、接收和拒绝都会写入办理记录（审计时间线）。
- 主办通过`transfer`指定`new_lead`发起转交；接收前原主办照常办理，新代理人暂无权操作。新代理人`accept`后身份立即切换，旧主办进入`former_leads`并保留历史查看；`decline`则案件归还原主办。已归档（`closed`）案件不能再调整分工。

转交示例：

```bash
curl -X POST /api/records/1/actions/transfer -H "X-User-Id: lawyer-lead" -H "X-Role: legal_rep" \
  -d '{"expected_version":3,"data":{"new_lead":"lawyer-new","note":"离职交接"}}'
curl -X POST /api/records/1/actions/accept -H "X-User-Id: lawyer-new" -H "X-Role: legal_rep" \
  -d '{"expected_version":4,"data":{}}'
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。

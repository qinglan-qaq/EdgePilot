# API 参考

- Base URL：`http://127.0.0.1:8000/api/v1`
- 在线文档：`http://127.0.0.1:8000/docs`（Swagger UI）、
  `http://127.0.0.1:8000/openapi.json`
- 健康检查：`GET /health`；Prometheus 指标：`GET /metrics`（不在 `/api/v1` 下）

## 1. 通用约定

### 1.1 认证

除登录/刷新外，所有接口要求：

```http
Authorization: Bearer <access_token>
```

登录：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"admin@edgepilot.local","password":"edgepilot-demo"}'
```

### 1.2 响应信封

```json
{
  "code": 0,
  "message": "ok",
  "data": {},
  "trace_id": "8f3c..."
}
```

失败时 HTTP 状态码与 `code` 一致；`trace_id` 用于在日志/Trace Explorer 中定位。
常见错误：

| 状态 | 场景 |
|---|---|
| 400 | 参数组合非法、状态不允许 |
| 401 | 缺少/过期 token、登录失败 |
| 403 | 角色越权、审批人不是当前用户 |
| 404 | 资源不存在或跨租户访问 |
| 409 | 唯一约束冲突、已发布版本重复、存在运行记录禁止删除 |
| 422 | 请求体校验失败（FastAPI 默认结构） |
| 503 | MQTT/外部依赖不可用（如节点命令下发） |

### 1.3 分页

列表接口统一使用 `page`（从 1 开始）与 `size`（默认 20）；响应：

```json
{"items": [], "total": 0, "page": 1, "size": 20}
```

## 2. 认证与用户

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/auth/login` | 公开 | 邮箱密码登录，返回 access/refresh |
| POST | `/auth/refresh` | 公开 | 用 refresh token 换新 access |
| POST | `/auth/logout` | 登录 | 将当前 access token 加入拒绝列表 |
| GET | `/auth/me` | 登录 | 当前用户与角色 |
| GET | `/users` | admin | 用户列表 |
| POST | `/users` | admin | 创建用户 |
| PATCH | `/users/{user_id}` | admin | 修改角色/状态/显示名/密码 |

## 3. 总览与审计

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/dashboard/summary` | 登录 | Agent/工作流/运行/事件/节点/设备/知识库计数、24h 事件趋势、最近运行 |
| GET | `/audit-logs` | admin | 审计日志，支持资源、动作、时间筛选 |

## 4. 节点、设备与事件

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/nodes` | 登录 | 边缘节点列表（状态、心跳、指标） |
| GET | `/nodes/{node_key}` | 登录 | 节点详情 |
| GET | `/nodes/{node_key}/metrics` | 登录 | 最近心跳指标 |
| PATCH | `/nodes/{node_key}` | admin/operator | 修改名称/标签/启停用 |
| POST | `/nodes/{node_key}/commands` | admin/operator | 下发 `start_task`/`stop_task`/`probe_device`/`sync_config`，写审计 |
| GET | `/devices` | 登录 | 设备/相机列表 |
| POST | `/devices` | admin/operator | 创建设备 |
| PATCH | `/devices/{device_id}` | admin/operator | 修改设备 |
| DELETE | `/devices/{device_id}` | admin/operator | 删除设备 |
| POST | `/devices/{device_id}/probe` | admin/operator | 触发 RTSP/抓帧探测（dry-run 语义，写审计） |
| GET | `/events` | 登录 | 事件列表，支持 `start/end/node_id/device_id/event_type/severity/status` |
| GET | `/events/{event_id}` | 登录 | 事件详情 |
| POST | `/events/{event_id}/ack` | admin/operator | 确认事件，写审计 |
| GET | `/events/{event_id}/snapshot` | 登录 | 读取截图；路径必须落在 `storage_dir` 内 |

事件写入不走 HTTP，而是 edge-agent 通过 MQTT 上行：

```
topic: edge/{node_key}/event
payload: {
  "msg_id": "...", "sent_at": "...", "node_key": "...",
  "event_type": "zone|line|dwell|count|anomaly",
  "occurred_at": "...", "severity": "low|medium|high",
  "device_id": 1, "payload": {"simulated": true}
}
```

## 5. Agent 与工具

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/agents` | 登录 | Agent 列表 |
| POST | `/agents` | admin | 创建 Agent |
| GET | `/agents/{agent_id}` | 登录 | 详情 |
| PATCH | `/agents/{agent_id}` | admin | 修改；已有运行记录的 Agent 不能硬删 |
| DELETE | `/agents/{agent_id}` | admin | 删除；存在运行记录返回 409 |
| POST | `/agents/{agent_id}/publish` | admin | 发布新版本（只追加） |
| GET | `/agents/{agent_id}/versions` | 登录 | 版本列表 |
| POST | `/agents/{agent_id}/test` | admin | 单 Agent 试跑；`agent_type=supervisor` 时跑 6 Agent 编排 |
| GET | `/agents/{agent_id}/runs` | 登录 | 运行记录 |
| GET | `/tools` | 登录 | 工具注册表（schema、权限范围、副作用） |

`POST /agents/{id}/test` 示例：

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/agents/1/test \
  -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"task":{"question":"现场设备是否正常"}}'
```

## 6. 工作流与运行

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/workflows` | 登录 | 工作流列表 |
| POST | `/workflows` | admin/operator | 创建工作流与草稿版本 0 |
| GET | `/workflows/{workflow_id}` | 登录 | 详情（DSL、当前版本） |
| PATCH | `/workflows/{workflow_id}` | admin/operator | 更新草稿 |
| POST | `/workflows/{workflow_id}/validate` | 登录 | 校验 DSL（节点、边、Agent 引用） |
| POST | `/workflows/{workflow_id}/publish` | admin | 发布不可变版本 |
| GET | `/workflows/{workflow_id}/versions` | 登录 | 版本列表 |
| POST | `/workflows/{workflow_id}/runs` | admin/operator | 触发运行 |
| GET | `/runs` | 登录 | 运行列表（状态/工作流/时间） |
| GET | `/runs/{run_id}` | 登录 | 运行详情、节点状态、输入输出 |
| GET | `/runs/{run_id}/logs` | 登录 | 节点日志/事件 |
| POST | `/runs/{run_id}/approve` | 登录 | 审批通过；必须是该节点 approvers 之一 |
| POST | `/runs/{run_id}/reject` | 登录 | 驳回 |
| POST | `/runs/{run_id}/cancel` | admin/operator | 取消运行 |

## 7. 知识库

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| POST | `/knowledge/documents` | admin/operator | 上传文档、解析、分块、入库 |
| GET | `/knowledge/documents` | 登录 | 文档/分块列表 |
| DELETE | `/knowledge/documents/{doc_id}` | admin/operator | 删除文档及其分块 |
| POST | `/knowledge/search` | 登录 | 混合检索（pgvector + 全文/trigram，可重排） |

## 8. 模型配置

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/models` | 登录 | 模型配置列表；`api_key_enc` 不回显 |
| POST | `/models` | admin | 创建；密钥加密存储 |
| GET | `/models/{model_id}` | 登录 | 详情 |
| PATCH | `/models/{model_id}` | admin | 修改端点/价格/状态 |
| DELETE | `/models/{model_id}` | admin | 删除 |
| POST | `/models/{model_id}/test` | admin | 连通性测试 |

## 9. 可观测性

### 9.1 Trace

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/observability/traces` | 列表；支持按状态、来源、Agent、时间、trace_id 搜索 |
| GET | `/observability/traces/{trace_id}` | 汇总（耗时、token、cost、span/error 数） |
| GET | `/observability/traces/{trace_id}/tree` | 嵌套 span 树 |
| GET | `/observability/traces/{trace_id}/spans` | 平铺 span |
| GET | `/observability/traces/{trace_id}/events` | trace 事件 |
| GET | `/observability/traces/{trace_id}/stream` | SSE 实时更新 |
| GET | `/observability/spans/{span_id}` | span 详情 |
| GET | `/observability/spans/{span_id}/children` | 子 span |

### 9.2 Analytics

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/observability/analytics/overview` | 指标卡汇总 |
| GET | `/observability/analytics/latency` | 延迟分位与趋势 |
| GET | `/observability/analytics/tokens` | token 趋势/分布 |
| GET | `/observability/analytics/cost` | 成本与价格覆盖率（缺价单列） |
| GET | `/observability/analytics/agents` | Agent 维度指标 |
| GET | `/observability/analytics/tools` | 工具调用成功率/耗时 |
| GET | `/observability/analytics/models` | 模型维度指标 |

### 9.3 Prompt、Playground 与设置

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/observability/prompts` | 登录 | 模板列表 |
| POST | `/observability/prompts` | admin | 创建模板与 v1 |
| GET | `/observability/prompts/{template_pk}` | 登录 | 模板详情与版本 |
| POST | `/observability/prompts/{template_pk}/versions` | admin | 追加版本 |
| POST | `/observability/prompts/{template_pk}/publish` | admin | 发布指定版本 |
| POST | `/observability/prompts/{template_pk}/rollback` | admin | 回滚到历史版本（生成新版本） |
| GET | `/observability/prompts/{template_pk}/executions` | 登录 | 渲染/执行记录 |
| POST | `/observability/prompts/{template_pk}/playground` | 登录 | 变量渲染 + Mock/真实模型试跑 |
| GET | `/observability/settings` | admin | 观测配置只读视图（采样、模式、保留） |

## 10. 错误处理建议

- 统一读取 `code` 与 `message`；`trace_id` 可直接打开 Trace Explorer 搜索。
- 404 同时表示“不存在”和“跨租户不可见”，调用方不要据此推断资源是否存在。
- 429/限流：登录与敏感写接口有令牌桶；按 `Retry-After` 退避。
- 长任务（工作流）返回运行 ID，不在 HTTP 请求内等待完成；通过 `/runs/{id}` 或 SSE 观察。

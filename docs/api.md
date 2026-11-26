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

## 10. 视觉识别（Vision Center）

前缀 `/vision`，共 14 个后端路由。读接口任意登录角色可用；写接口限
admin/operator；视频路由只认签名 token，不需要 `Authorization` 头。
`VISION_ENABLED=false` 时全部 `/vision/*`（含无 Bearer 的视频路由）返回
`503 vision_disabled`，其它模块不受影响。

| 方法 | 路径 | 权限 | 说明 |
|---|---|---|---|
| GET | `/vision/health` | 登录 | 聚合推理服务与模型状态；服务不可达仍 200，`reachable=false` |
| GET | `/vision/overview` | 登录 | 任务/完成/运行/失败数、处理帧数、检测数、事件数、最近 5 个任务 |
| GET | `/vision/models` | 登录 | DB 模型登记 + 推理服务实时状态；不可达时 `live.status="unreachable"`，仍返回 DB 许可事实 |
| GET | `/vision/tasks` | 登录 | 分页列表；支持 `status/q/start/end` |
| GET | `/vision/tasks/{task_uid}` | 登录 | 详情、结果汇总与 1 小时有效的签名 `video_url` |
| GET | `/vision/tasks/{task_uid}/results` | 登录 | 帧级结果；支持 `start_ms/end_ms/class_name/min_confidence/limit/offset` |
| GET | `/vision/tasks/{task_uid}/events` | 登录 | 该任务产生的视觉事件（分页） |
| POST | `/vision/tasks` | admin/operator | multipart 上传；`file` + 可选 `conf_threshold/frame_stride/target_samples/zones`；同视频同参数幂等返回 `deduplicated=true` |
| POST | `/vision/tasks/{task_uid}/cancel` | admin/operator | 尽力通知推理服务；不可达时仍在平台侧置 `cancelled`，终态幂等 |
| POST | `/vision/tasks/{task_uid}/retry` | admin/operator | 失败/取消后重新派发同一任务与视频，不新建任务 |
| POST | `/vision/tasks/{task_uid}/trigger` | admin/operator | 受控重触发工作流联动；复用任务级幂等运行 |
| POST | `/vision/tasks/{task_uid}/analyze` | admin/operator | 用任务最新事件做 Agent 研判；成功后同任务复用运行 |
| POST | `/vision/events/{event_id}/analyze` | admin/operator | 研判指定事件（归属其任务），规则与任务级一致 |
| GET | `/vision/tasks/{task_uid}/video?token=…&expires=…` | 签名 | 视频流；支持 Range（206），签名/租户/任务/文件任一不符 403 |

关键错误码（`code` 等于 HTTP 状态码，原因在 `data.reason`）：

| HTTP | `data.reason` | 场景 |
|---|---|---|
| 400 | `invalid_file_type` / `invalid_zones` | 后缀/魔数不合法、区域结构或坐标非法 |
| 403 | `invalid_media_token` | 视频签名缺失、过期、租户/任务/文件指纹不符 |
| 404 | `task_not_found` / `event_not_found` / `artifact_missing` / `video_missing` | 不存在、跨租户，或结果/视频文件已被清理 |
| 404 | `result_not_found` | 研判时结果尚未生成 |
| 409 | `task_not_retryable` / `retry_limit_exceeded` | 状态不允许重试、达到 `VISION_MAX_RETRIES` |
| 409 | `result_not_found` / `trigger_pending` | 重触发时结果未生成或联动仍在 pending |
| 409 | `no_vision_events` / `vision_agent_missing` / `vision_agent_running` | 无事件可研判、无可用 Vision Agent、研判进行中 |
| 409 | `vision_agent_disabled` | `VISION_AGENT_ENABLED=false` 时调用研判接口 |
| 409 | `upload_conflict` | 同 dedup_key 并发上传竞争后仍读不到赢家任务，需重试 |
| 413 | `file_too_large` | 超过 `VISION_MAX_UPLOAD_MB` |
| 422 | `video_too_long` / `invalid_parameter` / `artifact_unreadable` | 时长超限（probe 阶段拒绝）、`frame_stride`/`target_samples` 超过配置上限、结果文件结构非法 |
| 503 | `vision_disabled` / `artifact_corrupted` | 功能关闭、artifact 哈希不符 |
| 507 | `storage_write_failed` | 视频/结果写入磁盘失败 |

任务级 `error_code` 独立于 HTTP：`video_missing`、`decode_failed`、`empty_video`、
`model_unavailable`、`inference_failed`、`queue_full`、`service_shutting_down`、
`timeout`、`cancelled`、`service_restarted`、`vision_job_lost`、
`vision_service_unavailable`、`vision_failed`、`artifact_unreadable`、`purged`。

### 10.1 签名视频与 Range

- 详情响应返回 `/api/v1/vision/tasks/{uid}/video?token=…&expires=…`；token 是
  HMAC-SHA256 签名，绑定租户、任务与视频 SHA-256，默认 1 小时有效。
- `<video>` 标签直接使用该 URL；后端返回 `Accept-Ranges: bytes` 与
  `Cache-Control: private, max-age=3600`。
- 带 `Range` 的请求返回 `206` + `Content-Range`；越界 Range 返回 `416`；
  不支持 Range 的客户端回退 `200` 全量。
- 签名错误统一 `403 invalid_media_token`，不区分“token 错”和“任务不存在”。

### 10.2 推理服务内部路由（vision-service，默认 `127.0.0.1:8001`）

这些路由仅供后端调用，不经过 `/api/v1`；配置 `VISION_SERVICE_TOKEN` 后需带
`X-Vision-Token` 头（为空表示本地不校验）。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 服务/队列/存储可写/模型加载状态；权重缺失时为 `degraded` |
| GET | `/v1/models` | 当前模型名、版本、设备、类别、许可与 `load_status` |
| POST | `/v1/models/reload` | 重新加载权重（复制权重进运行中容器后必须调用） |
| POST | `/v1/probe` | 探测视频时长/帧率/分辨率，用于上传前校验 |
| POST | `/v1/jobs` | 创建推理任务，返回 202 与作业状态 |
| GET | `/v1/jobs/{task_uid}` | 作业状态与进度；不存在返回 404 `job_not_found` |
| POST | `/v1/jobs/{task_uid}/cancel` | 取消作业；不存在返回 404 |
| GET | `/v1/jobs/{task_uid}/results` | 读取 artifact；未完成 409 `result_not_ready`，文件丢失 410 `result_missing` |

## 11. 错误处理建议

- 统一读取 `code` 与 `message`；`trace_id` 可直接打开 Trace Explorer 搜索。
- 404 同时表示“不存在”和“跨租户不可见”，调用方不要据此推断资源是否存在。
- 429/限流：登录与敏感写接口有令牌桶；按 `Retry-After` 退避。
- 长任务（工作流）返回运行 ID，不在 HTTP 请求内等待完成；通过 `/runs/{id}` 或 SSE 观察。

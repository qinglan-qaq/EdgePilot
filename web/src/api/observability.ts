import { api } from '@/api/http'

/** 十进制费用可能以字符串返回（避免浮点精度损失），未知时为 null。 */
export type DecimalLike = string | number | null

/** Trace 列表/详情共有的汇总字段。 */
export interface TraceSummary {
  trace_id: string
  name: string
  source: string
  status: string
  start_time: string | null
  end_time: string | null
  duration_ms: number | null
  total_spans: number
  error_count: number
  total_tokens: number | null
  total_cost: DecimalLike
}

/** GET /observability/traces 的分页响应。 */
export interface TracePage {
  items: TraceSummary[]
  total: number
  page: number
  size: number
}

/** GET /observability/traces 的查询参数；布尔筛选按真实布尔值发送。 */
export interface ListTracesParams {
  page?: number
  size?: number
  status?: string
  /** trace_id 前缀匹配。 */
  trace_id?: string
  source?: string
  user_id?: string
  /** 存在该 agent span（span_type='agent'）的 trace。 */
  agent_id?: number
  /** llm_calls.model_name 或 provider 精确匹配。 */
  model?: string
  /** tool_calls.tool_id 或 tool_name 精确匹配。 */
  tool?: string
  start_from?: string
  start_to?: string
  min_duration_ms?: number
  /** true 时仅返回 failed / timeout / cancelled。 */
  failed?: boolean
  /** true 时仅返回 duration_ms >= 3000 的 trace。 */
  slow?: boolean
}

/** GET /observability/traces/{trace_id} 的完整详情。 */
export interface TraceDetail extends TraceSummary {
  id: number
  user_id: number | null
  session_id: string | null
  root_span_id: string | null
  input: unknown
  output: unknown
  cost_unavailable_count: number | null
  meta: Record<string, unknown> | null
  created_at: string | null
}

/** Trace 树节点；children 由后端树接口递归返回。 */
export interface SpanNode {
  id?: number
  span_id: string
  parent_span_id?: string | null
  name: string
  span_type: string
  status: string
  attempt?: number | null
  agent_id?: number | null
  agent_version?: number | null
  workflow_id?: number | null
  node_id?: string | null
  tool_id?: string | null
  start_time?: string | null
  end_time?: string | null
  duration_ms?: number | null
  input?: unknown
  output?: unknown
  error?: unknown
  retry_count?: number | null
  meta?: unknown
  links?: unknown
  created_at?: string | null
  orphan?: boolean
  children?: SpanNode[]
  [key: string]: unknown
}

/** LLM 调用记录；cost_available 为 false 时费用不可用，绝不估算。 */
export interface LLMCallRecord {
  id?: number
  provider?: string | null
  model_name?: string | null
  model_version?: string | null
  total_tokens?: number | null
  prompt_tokens?: number | null
  completion_tokens?: number | null
  cost?: DecimalLike
  cost_available?: boolean | null
  [key: string]: unknown
}

/** 工具调用记录。 */
export interface ToolCallRecord {
  id?: number
  tool_id?: string | null
  tool_name?: string | null
  execution_status?: string | null
  duration_ms?: number | null
  retry_count?: number | null
  error?: unknown
  [key: string]: unknown
}

/** 检索记录。 */
export interface RetrievalRecord {
  id?: number
  query?: string | null
  rewritten_query?: string | null
  retrieval_strategy?: string | null
  embedding_model?: string | null
  vector_store?: string | null
  reranker_model?: string | null
  top_k?: number | null
  similarity_threshold?: number | null
  retrieved_documents?: unknown[] | null
  reranked_documents?: unknown[] | null
  final_context?: string | null
  duration_ms?: number | null
  error?: unknown
  [key: string]: unknown
}

/** Prompt 执行记录；只读展示，缺失字段保持 null，不做补齐。 */
export interface PromptExecutionRecord {
  id?: number
  trace_id?: string | null
  span_id?: string | null
  template_id?: number | null
  template_version?: number | null
  variables?: unknown
  rendered_messages?: unknown
  prompt_hash?: string | null
  render_status?: string | null
  model_name?: string | null
  created_at?: string | null
  [key: string]: unknown
}

/** trace 事件；sequence_number 在同一 trace 内唯一且按升序展示。 */
export interface SpanEvent {
  id: number
  trace_id: string
  span_id: string | null
  event_type: string
  event_data: unknown
  sequence_number: number
  created_at: string | null
}

/** GET /observability/spans/{span_id} 的详情。 */
export interface SpanDetail extends SpanNode {
  input?: unknown
  output?: unknown
  llm_calls?: LLMCallRecord[]
  tool_calls?: ToolCallRecord[]
  /**
   * 检索记录。后端当前返回键名为 retrieval_records，接口约定中称 retrievals；
   * 两个键均做兼容读取，缺失时视为空数组，不虚构记录。
   */
  retrievals?: RetrievalRecord[]
  retrieval_records?: RetrievalRecord[]
  /** Prompt 执行记录；表缺失或没有记录时为空数组。 */
  prompt_executions?: PromptExecutionRecord[]
}

function tracePath(traceId: string): string {
  return `/observability/traces/${encodeURIComponent(traceId)}`
}

/**
 * 分页读取 Trace 列表。筛选全部交给后端完成，前端不得拉全量再过滤；
 * 调用方负责只传入有值的字段。
 */
export function listTraces(params: ListTracesParams = {}): Promise<TracePage> {
  return api.get<TracePage>('/observability/traces', { params })
}

/** 读取 Trace 汇总详情。 */
export function getTrace(traceId: string): Promise<TraceDetail> {
  return api.get<TraceDetail>(tracePath(traceId))
}

/** 读取 Trace 的 Span 树（根节点数组，children 递归）。 */
export function getTraceTree(traceId: string): Promise<SpanNode[]> {
  return api.get<SpanNode[]>(`${tracePath(traceId)}/tree`)
}

/** 读取 Trace 的扁平 Span 列表（时间线用），后端按 start_time、id 升序。 */
export function listTraceSpans(traceId: string): Promise<SpanNode[]> {
  return api.get<SpanNode[]>(`${tracePath(traceId)}/spans`)
}

/** 读取 Trace 的全部事件，后端按 sequence_number 升序。 */
export function listTraceEvents(traceId: string): Promise<SpanEvent[]> {
  return api.get<SpanEvent[]>(`${tracePath(traceId)}/events`)
}

/** 读取 Span 详情及关联的 LLM / 工具 / 检索记录。 */
export function getSpan(spanId: string): Promise<SpanDetail> {
  return api.get<SpanDetail>(`/observability/spans/${encodeURIComponent(spanId)}`)
}

// ---------------------------------------------------------------------------
// 分析（analytics）接口契约
//
// 所有聚合都在后端 SQL 内完成，前端不得拉全量再算。查询参数为可选 ISO
// datetime；不传时后端按最近 24 小时解析，并在 overview.window 中回显。
// ---------------------------------------------------------------------------

/** 分析接口共用的时间窗口查询参数；缺省由后端补全最近 24 小时。 */
export interface AnalyticsQuery {
  start_from?: string
  start_to?: string
}

/** 趋势分桶粒度：窗口 <= 72 小时按小时，否则按天。 */
export type AnalyticsBucket = 'hour' | 'day'

/** 后端解析后的实际窗口（ISO datetime 字符串）。 */
export interface AnalyticsWindow {
  start_from: string
  start_to: string
  bucket: AnalyticsBucket
}

/** POST /analytics/overview 的 10 个指标卡。 */
export interface OverviewCards {
  traces_total: number
  /** succeeded / 已结束 trace；无已结束 trace 时为 null。 */
  trace_success_rate: number | null
  trace_p95_ms: number | null
  spans_total: number
  agent_runs: number
  /** 已结束逻辑 tool span 的 succeeded 占比；无已结束调用时为 null。 */
  tool_success_rate: number | null
  tool_permission_denied: number
  llm_calls: number
  /** 全部调用缺 usage 时为 null，绝不用 0 代替。 */
  total_tokens: number | null
  /** 全部调用缺价时为 null，绝不估算。 */
  total_cost: DecimalLike
  cost_unavailable_count: number
  cost_coverage: {
    priced_models: number
    total_models: number
  }
}

/** trace_trend：按窗口分桶统计 trace 总数与成功/失败数。 */
export interface TraceTrendPoint {
  bucket: string
  total: number
  succeeded: number
  failed: number
}

/** latency_trend：按窗口分桶统计 trace 耗时分位；无数据时为 null。 */
export interface LatencyTrendPoint {
  bucket: string
  p50_ms: number | null
  p95_ms: number | null
}

/** token_trend：按窗口分桶汇总 token；缺 usage 时为 null。 */
export interface TokenTrendPoint {
  bucket: string
  total_tokens: number | null
  cached_tokens: number | null
}

/** model_distribution：provider + model_name 维度的调用、token 与成本。 */
export interface ModelDistributionItem {
  provider: string | null
  model_name: string | null
  calls: number
  total_tokens: number | null
  cost: number | null
  cost_unavailable_count: number
}

/** agent_distribution：逻辑 agent span 维度的执行数、失败数与 P95。 */
export interface AgentDistributionItem {
  label: string | null
  runs: number
  failed: number
  p95_ms: number | null
}

/** tool_distribution：逻辑 tool span 维度的调用数与成功率。 */
export interface ToolDistributionItem {
  tool_id: string
  tool_name: string | null
  calls: number
  success_rate: number | null
  p95_ms: number | null
}

/** error_distribution：按 span error.code 分组。 */
export interface ErrorDistributionItem {
  code: string
  count: number
}

/** slowest_traces：时间窗内耗时降序 Top 10。 */
export interface SlowestTraceItem {
  trace_id: string
  name: string | null
  status: string
  duration_ms: number | null
  start_time: string | null
}

/** GET /observability/analytics/overview 的图表区。 */
export interface OverviewCharts {
  trace_trend: TraceTrendPoint[]
  latency_trend: LatencyTrendPoint[]
  token_trend: TokenTrendPoint[]
  model_distribution: ModelDistributionItem[]
  agent_distribution: AgentDistributionItem[]
  tool_distribution: ToolDistributionItem[]
  error_distribution: ErrorDistributionItem[]
  slowest_traces: SlowestTraceItem[]
}

/** GET /observability/analytics/overview 的完整响应。 */
export interface AnalyticsOverview {
  window: AnalyticsWindow
  cards: OverviewCards
  charts: OverviewCharts
}

/** GET /observability/analytics/latency 的响应。 */
export interface AnalyticsLatency {
  trace: {
    count: number
    p50_ms: number | null
    p95_ms: number | null
  }
  by_span_type: Array<{
    span_type: string | null
    count: number
    p50_ms: number | null
    p95_ms: number | null
  }>
}

/** GET /observability/analytics/tokens 的响应。 */
export interface AnalyticsTokens {
  total_tokens: number | null
  prompt_tokens: number | null
  completion_tokens: number | null
  cached_tokens: number | null
  cached_ratio: number | null
  unavailable_calls: number
  by_model: Array<{
    provider: string | null
    model_name: string | null
    calls: number
    prompt_tokens: number | null
    completion_tokens: number | null
    cached_tokens: number | null
    total_tokens: number | null
  }>
}

/** GET /observability/analytics/cost 的响应。 */
export interface AnalyticsCost {
  total_cost: number | null
  cost_unavailable_count: number
  coverage: {
    priced_models: number
    total_models: number
  }
  by_model: Array<{
    provider: string | null
    model_name: string | null
    calls: number
    cost: number | null
    cost_unavailable_count: number
  }>
}

/** GET /observability/analytics/agents 的响应项。 */
export interface AgentAnalyticsItem {
  agent_id: number | null
  label: string | null
  runs: number
  failed: number
  success_rate: number | null
  avg_duration_ms: number | null
  p95_duration_ms: number | null
  total_tokens: number | null
  tool_calls: number
}

/** GET /observability/analytics/agents 的响应。 */
export interface AnalyticsAgents {
  items: AgentAnalyticsItem[]
}

/** GET /observability/analytics/tools 的响应项。 */
export interface ToolAnalyticsItem {
  tool_id: string
  tool_name: string | null
  calls: number
  success_rate: number | null
  p50_ms: number | null
  p95_ms: number | null
  retry_count: number
  permission_denied: number
}

/** GET /observability/analytics/tools 的响应。 */
export interface AnalyticsTools {
  items: ToolAnalyticsItem[]
}

/** GET /observability/analytics/models 的响应项。 */
export interface ModelAnalyticsItem {
  provider: string | null
  model_name: string | null
  calls: number
  prompt_tokens: number | null
  completion_tokens: number | null
  cached_tokens: number | null
  total_tokens: number | null
  cost: number | null
  cost_unavailable_count: number
  errors: number
}

/** GET /observability/analytics/models 的响应。 */
export interface AnalyticsModels {
  items: ModelAnalyticsItem[]
}

/**
 * 只保留实际存在的查询键，避免把 undefined 序列化进 URL。
 * 返回空对象时 axios 不会追加 query string，等同于默认窗口。
 */
function analyticsParams(query?: AnalyticsQuery): Record<string, string> {
  const params: Record<string, string> = {}
  if (query?.start_from) {
    params.start_from = query.start_from
  }
  if (query?.start_to) {
    params.start_to = query.start_to
  }
  return params
}

const ANALYTICS_BASE = '/observability/analytics'

/** 读取 Overview 指标卡与 8 类图表。 */
export function getAnalyticsOverview(query?: AnalyticsQuery): Promise<AnalyticsOverview> {
  return api.get<AnalyticsOverview>(`${ANALYTICS_BASE}/overview`, {
    params: analyticsParams(query),
  })
}

/** 读取延迟分析（trace 分位 + span_type 分组）。 */
export function getAnalyticsLatency(query?: AnalyticsQuery): Promise<AnalyticsLatency> {
  return api.get<AnalyticsLatency>(`${ANALYTICS_BASE}/latency`, {
    params: analyticsParams(query),
  })
}

/** 读取 token 分析（总量、缓存占比、按模型）。 */
export function getAnalyticsTokens(query?: AnalyticsQuery): Promise<AnalyticsTokens> {
  return api.get<AnalyticsTokens>(`${ANALYTICS_BASE}/tokens`, {
    params: analyticsParams(query),
  })
}

/** 读取成本分析（总额、缺价次数、覆盖模型、按模型）。 */
export function getAnalyticsCost(query?: AnalyticsQuery): Promise<AnalyticsCost> {
  return api.get<AnalyticsCost>(`${ANALYTICS_BASE}/cost`, {
    params: analyticsParams(query),
  })
}

/** 读取 Agent 分析。 */
export function getAnalyticsAgents(query?: AnalyticsQuery): Promise<AnalyticsAgents> {
  return api.get<AnalyticsAgents>(`${ANALYTICS_BASE}/agents`, {
    params: analyticsParams(query),
  })
}

/** 读取工具分析。 */
export function getAnalyticsTools(query?: AnalyticsQuery): Promise<AnalyticsTools> {
  return api.get<AnalyticsTools>(`${ANALYTICS_BASE}/tools`, {
    params: analyticsParams(query),
  })
}

/** 读取模型分析。 */
export function getAnalyticsModels(query?: AnalyticsQuery): Promise<AnalyticsModels> {
  return api.get<AnalyticsModels>(`${ANALYTICS_BASE}/models`, {
    params: analyticsParams(query),
  })
}

// ---------------------------------------------------------------------------
// Prompt 模板 / 不可变版本 / 执行历史 / Playground
//
// 版本行不可变：新增版本只能追加。所有接口的模板主键都是数字 id
// （后端字段 template_pk），不是字符串 template_id。
// ---------------------------------------------------------------------------

/** 模板分页列表项。 */
export interface PromptTemplateSummary {
  id: number
  template_id: string
  template_name: string
  category: string | null
  active_version: number | null
  status: string
  updated_at: string | null
  created_at: string | null
}

/** GET /observability/prompts 的分页响应。 */
export interface PromptTemplatePage {
  items: PromptTemplateSummary[]
  total: number
  page: number
  size: number
}

/** 不可变版本行；缺失字段保持 null，不做补齐。 */
export interface PromptVersion {
  id: number
  /** 数字模板主键（对应列表项的 id）。 */
  template_id: number
  version: number
  system_prompt: string | null
  user_prompt_template: string | null
  variables_schema: Record<string, unknown> | null
  model_config: Record<string, unknown> | null
  changelog: string | null
  created_by: number | null
  created_at: string | null
}

/** GET /observability/prompts/{template_pk} 的详情（含全部版本）。 */
export interface PromptTemplateDetail extends PromptTemplateSummary {
  description: string | null
  owner: string | null
  versions: PromptVersion[]
}

/** POST /observability/prompts 的响应。 */
export interface PromptTemplateCreated {
  id: number
  template_id: string
  template_name: string
  active_version: number | null
  status: string
}

/** 创建模板的请求体。 */
export interface CreatePromptTemplatePayload {
  template_id: string
  template_name: string
  description?: string | null
  category?: string | null
  owner?: string | null
}

/** 追加版本的请求体；未填写的字段保持 null，由后端原样落库。 */
export interface AddPromptVersionPayload {
  system_prompt?: string | null
  user_prompt_template?: string | null
  variables_schema?: Record<string, unknown> | null
  model_config?: Record<string, unknown> | null
  changelog?: string | null
}

/** Playground 请求体；execute=true 需要 admin/operator 且会真实调用 LLM。 */
export interface PromptPlaygroundPayload {
  version?: number | null
  variables?: Record<string, unknown>
  execute?: boolean
  model_config_id?: number | null
}

/** Playground 真实执行时 LLM 返回的原始字段；缺失值保持 null。 */
export interface PromptPlaygroundResponse {
  content: string | null
  structured: unknown
  usage: unknown
  finish_reason: string | null
  provider: string | null
  model_name: string | null
  request_id: string | null
  cost: DecimalLike
  error_code: string | null
  degraded: boolean | null
}

/** POST /observability/prompts/{template_pk}/playground 的响应。 */
export interface PromptPlaygroundResult {
  status: string
  messages: unknown[]
  prompt_hash: string | null
  version: number
  /** 预览模式没有 trace；渲染失败时同样为 null。 */
  trace_id?: string | null
  span_id?: string | null
  /** 真实执行的模型响应；预览或渲染失败时为 null。 */
  response?: PromptPlaygroundResponse | null
}

/** 执行历史项；缺失值保持 null。 */
export interface PromptExecutionItem {
  id: number
  trace_id: string | null
  span_id: string | null
  template_version: number | null
  render_status: string
  model_name: string | null
  prompt_hash: string | null
  variables: unknown
  rendered_messages: unknown
  created_at: string | null
}

/** GET /observability/prompts/{template_pk}/executions 的分页响应。 */
export interface PromptExecutionPage {
  items: PromptExecutionItem[]
  total: number
  page: number
  size: number
}

const PROMPTS_BASE = '/observability/prompts'

function promptTemplatePath(templatePk: number): string {
  return `${PROMPTS_BASE}/${encodeURIComponent(String(templatePk))}`
}

/** 分页读取 Prompt 模板列表；不传参数时由后端使用默认分页。 */
export function listPromptTemplates(
  params: { page?: number; size?: number } = {},
): Promise<PromptTemplatePage> {
  return api.get<PromptTemplatePage>(PROMPTS_BASE, { params })
}

/** 读取模板详情与全部不可变版本（后端按版本号倒序）。 */
export function getPromptTemplate(templatePk: number): Promise<PromptTemplateDetail> {
  return api.get<PromptTemplateDetail>(promptTemplatePath(templatePk))
}

/** 创建模板（admin）。 */
export function createPromptTemplate(
  payload: CreatePromptTemplatePayload,
): Promise<PromptTemplateCreated> {
  return api.post<PromptTemplateCreated>(PROMPTS_BASE, payload)
}

/** 追加版本（admin）；版本号由后端在模板行锁内自增。 */
export function addPromptVersion(
  templatePk: number,
  payload: AddPromptVersionPayload,
): Promise<PromptVersion> {
  return api.post<PromptVersion>(`${promptTemplatePath(templatePk)}/versions`, payload)
}

/** 发布指定版本（admin），只修改 active_version。 */
export function publishPromptVersion(
  templatePk: number,
  version: number,
): Promise<{ active_version: number | null }> {
  return api.post<{ active_version: number | null }>(
    `${promptTemplatePath(templatePk)}/publish`,
    { version },
  )
}

/** 回滚到指定版本（admin），语义与发布一致。 */
export function rollbackPromptVersion(
  templatePk: number,
  version: number,
): Promise<{ active_version: number | null }> {
  return api.post<{ active_version: number | null }>(
    `${promptTemplatePath(templatePk)}/rollback`,
    { version },
  )
}

/** Playground：execute=false 仅渲染预览，execute=true 真实调用 LLM。 */
export function runPromptPlayground(
  templatePk: number,
  payload: PromptPlaygroundPayload,
): Promise<PromptPlaygroundResult> {
  return api.post<PromptPlaygroundResult>(
    `${promptTemplatePath(templatePk)}/playground`,
    payload,
  )
}

/** 分页读取模板执行历史（created_at、id 倒序）。 */
export function listPromptExecutions(
  templatePk: number,
  params: { page?: number; size?: number } = {},
): Promise<PromptExecutionPage> {
  return api.get<PromptExecutionPage>(`${promptTemplatePath(templatePk)}/executions`, {
    params,
  })
}

// ---------------------------------------------------------------------------
// 可观测设置（只读）
// ---------------------------------------------------------------------------

/** 缺价模型：cost IS NULL 的调用按 provider/model 聚合。 */
export interface UnpricedModelItem {
  provider: string
  model_name: string
  calls: number
}

/** 脱敏规则快照。 */
export interface ObservabilityRedaction {
  sensitive_key_pattern: string
  scrubbers: string[]
  token_count_exempt: boolean
}

/** GET /observability/settings 的响应（仅 admin）。 */
export interface ObservabilitySettings {
  capture_mode: string
  capture_modes: string[]
  retention_days: Record<string, number>
  redaction: ObservabilityRedaction
  unpriced_models: UnpricedModelItem[]
}

/** 读取当前进程的真实可观测配置。 */
export function getObservabilitySettings(): Promise<ObservabilitySettings> {
  return api.get<ObservabilitySettings>('/observability/settings')
}

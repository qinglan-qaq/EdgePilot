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
  span_id: string
  parent_span_id?: string | null
  name: string
  span_type: string
  status: string
  attempt?: number | null
  node_id?: string | null
  tool_id?: string | null
  start_time?: string | null
  end_time?: string | null
  duration_ms?: number | null
  error?: unknown
  retry_count?: number | null
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
  retrieval_strategy?: string | null
  vector_store?: string | null
  top_k?: number | null
  duration_ms?: number | null
  [key: string]: unknown
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
}

function tracePath(traceId: string): string {
  return `/observability/traces/${encodeURIComponent(traceId)}`
}

/** 读取 Trace 汇总详情。 */
export function getTrace(traceId: string): Promise<TraceDetail> {
  return api.get<TraceDetail>(tracePath(traceId))
}

/** 读取 Trace 的 Span 树（根节点数组，children 递归）。 */
export function getTraceTree(traceId: string): Promise<SpanNode[]> {
  return api.get<SpanNode[]>(`${tracePath(traceId)}/tree`)
}

/** 读取 Span 详情及关联的 LLM / 工具 / 检索记录。 */
export function getSpan(spanId: string): Promise<SpanDetail> {
  return api.get<SpanDetail>(`/observability/spans/${encodeURIComponent(spanId)}`)
}

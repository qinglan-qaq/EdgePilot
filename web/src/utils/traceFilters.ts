/**
 * Trace Explorer 的 URL 筛选契约。
 *
 * URL 是筛选条件的唯一来源：页面从 route.query 解析，组件只修改 URL，
 * 再由 watcher 统一发起请求。这样刷新、复制链接与浏览器前进/后退都能还原
 * 同一组筛选。空字段与 page=1 不写进 URL，保持链接可读。
 */

import type { ListTracesParams } from '@/api/observability'

/** 表单可编辑状态：数字字段保留原始字符串，构造请求时再安全转换。 */
export interface TraceFilterState {
  status: string
  /** trace_id 前缀 */
  traceId: string
  source: string
  userId: string
  agentId: string
  model: string
  tool: string
  startFrom: string
  startTo: string
  minDurationMs: string
  failed: boolean
  slow: boolean
}

/** 空筛选条件；每次使用前复制，避免共享对象被修改。 */
export const EMPTY_TRACE_FILTERS: TraceFilterState = {
  status: '',
  traceId: '',
  source: '',
  userId: '',
  agentId: '',
  model: '',
  tool: '',
  startFrom: '',
  startTo: '',
  minDurationMs: '',
  failed: false,
  slow: false,
}

/** 与 vue-router 的 LocationQuery 兼容的最小读取接口。 */
export type TraceQuery = Record<string, unknown>

function firstQueryValue(value: unknown): string {
  if (Array.isArray(value)) {
    return firstQueryValue(value[0])
  }
  if (typeof value === 'string') {
    return value
  }
  if (typeof value === 'number' && Number.isFinite(value)) {
    return String(value)
  }
  return ''
}

function queryFlag(value: unknown): boolean {
  const raw = firstQueryValue(value).trim()
  return raw === '1' || raw === 'true'
}

/** 解析筛选条件；缺失或非字符串值按空字符串处理。 */
export function parseTraceFilters(query: TraceQuery): TraceFilterState {
  return {
    status: firstQueryValue(query.status),
    traceId: firstQueryValue(query.trace_id),
    source: firstQueryValue(query.source),
    userId: firstQueryValue(query.user_id),
    agentId: firstQueryValue(query.agent_id),
    model: firstQueryValue(query.model),
    tool: firstQueryValue(query.tool),
    startFrom: firstQueryValue(query.start_from),
    startTo: firstQueryValue(query.start_to),
    minDurationMs: firstQueryValue(query.min_duration_ms),
    failed: queryFlag(query.failed),
    slow: queryFlag(query.slow),
  }
}

/** 解析页码；非法、非正整数一律回到第 1 页。 */
export function parseTracePage(query: TraceQuery): number {
  const value = Number(firstQueryValue(query.page).trim())
  if (!Number.isInteger(value) || value < 1) {
    return 1
  }
  return value
}

function trimmed(value: string): string {
  return value.trim()
}

function finiteNumber(value: string): number | undefined {
  const text = trimmed(value)
  if (!text) {
    return undefined
  }
  const parsed = Number(text)
  return Number.isFinite(parsed) ? parsed : undefined
}

function integerNumber(value: string): number | undefined {
  const parsed = finiteNumber(value)
  if (parsed === undefined || !Number.isInteger(parsed)) {
    return undefined
  }
  return parsed
}

/**
 * 构造列表请求参数：空值与非法数字不发送，布尔值是真实 boolean。
 * page / size 始终发送，后端据此分页。
 *
 * 关键节点：
 * 1. 只有用户确实填写的字段才进入请求，避免空串被后端当成筛选条件；
 * 2. 数字字段先做有限值校验，NaN / Infinity 不会发给后端；
 * 3. failed / slow 只有为 true 才发送，默认查询不受影响。
 *
 * @param filters 页面上的筛选状态。
 * @param page 页码，从 1 开始。
 * @param size 每页条数。
 * @returns 可直接传给 Trace 列表 API 的参数。
 */
export function buildTraceListParams(
  filters: TraceFilterState,
  page: number,
  size: number,
): ListTracesParams {
  const params: ListTracesParams = { page, size }

  const status = trimmed(filters.status)
  if (status) {
    params.status = status
  }
  const traceId = trimmed(filters.traceId)
  if (traceId) {
    params.trace_id = traceId
  }
  const source = trimmed(filters.source)
  if (source) {
    params.source = source
  }
  const userId = trimmed(filters.userId)
  if (userId) {
    params.user_id = userId
  }
  const agentId = integerNumber(filters.agentId)
  if (agentId !== undefined) {
    params.agent_id = agentId
  }
  const model = trimmed(filters.model)
  if (model) {
    params.model = model
  }
  const tool = trimmed(filters.tool)
  if (tool) {
    params.tool = tool
  }
  const startFrom = trimmed(filters.startFrom)
  if (startFrom) {
    params.start_from = startFrom
  }
  const startTo = trimmed(filters.startTo)
  if (startTo) {
    params.start_to = startTo
  }
  const minDurationMs = finiteNumber(filters.minDurationMs)
  if (minDurationMs !== undefined) {
    params.min_duration_ms = minDurationMs
  }
  if (filters.failed) {
    params.failed = true
  }
  if (filters.slow) {
    params.slow = true
  }

  return params
}

/**
 * 构造路由 query：省略空字段与 page=1；布尔写成 failed=1 / slow=1。
 *
 * 关键节点：URL 即筛选事实源，因此这里必须与 `parseTraceFilters`
 * 保持可逆：写入什么，刷新后就能还原成同样的筛选状态。
 *
 * @param filters 页面上的筛选状态。
 * @param page 当前页码。
 * @returns 可直接写进 router 的字符串 query。
 */
export function buildTraceRouteQuery(
  filters: TraceFilterState,
  page: number,
): Record<string, string> {
  const query: Record<string, string> = {}

  const status = trimmed(filters.status)
  if (status) {
    query.status = status
  }
  const traceId = trimmed(filters.traceId)
  if (traceId) {
    query.trace_id = traceId
  }
  const source = trimmed(filters.source)
  if (source) {
    query.source = source
  }
  const userId = trimmed(filters.userId)
  if (userId) {
    query.user_id = userId
  }
  const agentId = trimmed(filters.agentId)
  if (agentId) {
    query.agent_id = agentId
  }
  const model = trimmed(filters.model)
  if (model) {
    query.model = model
  }
  const tool = trimmed(filters.tool)
  if (tool) {
    query.tool = tool
  }
  const startFrom = trimmed(filters.startFrom)
  if (startFrom) {
    query.start_from = startFrom
  }
  const startTo = trimmed(filters.startTo)
  if (startTo) {
    query.start_to = startTo
  }
  const minDurationMs = trimmed(filters.minDurationMs)
  if (minDurationMs) {
    query.min_duration_ms = minDurationMs
  }
  if (filters.failed) {
    query.failed = '1'
  }
  if (filters.slow) {
    query.slow = '1'
  }
  if (Number.isInteger(page) && page > 1) {
    query.page = String(page)
  }

  return query
}

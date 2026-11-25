import { ref } from 'vue'
import { defineStore } from 'pinia'

import { ApiError } from '@/api/http'
import {
  getAnalyticsOverview,
  getSpan,
  getTrace,
  getTraceTree,
  listTraceEvents,
  listTraceSpans,
  listTraces,
  type AnalyticsOverview,
  type AnalyticsQuery,
  type ListTracesParams,
  type SpanDetail,
  type SpanEvent,
  type SpanNode,
  type TraceDetail,
  type TraceSummary,
} from '@/api/observability'

/** 只展示后端返回的错误信息，避免把 axios 细节暴露给用户。 */
function errorToMessage(value: unknown): string {
  if (value instanceof ApiError) {
    return value.message
  }
  if (value instanceof Error && value.message) {
    return value.message
  }
  return '请求失败，请稍后重试'
}

/**
 * 可观测状态仓库，按域拆分。
 * - overview：概览指标卡与图表（Task 14）。
 * - traces：Trace Explorer 列表（Task 15）；请求参数全部由页面从 URL 解析。
 * - traceDetail：Trace 三栏详情（Task 16）；树/扁平 Span/事件并行加载，
 *   选中 Span 后按 span_id 懒加载详情。
 */
export const useObservabilityStore = defineStore('observability', () => {
  // ---- Overview ---------------------------------------------------------
  const overview = ref<AnalyticsOverview | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  // ---- Trace 列表 -------------------------------------------------------
  const traces = ref<TraceSummary[]>([])
  const tracesTotal = ref(0)
  const tracesLoading = ref(false)
  const tracesError = ref<string | null>(null)

  // ---- Trace 详情 -------------------------------------------------------
  const traceDetail = ref<TraceDetail | null>(null)
  const traceTree = ref<SpanNode[]>([])
  const traceSpans = ref<SpanNode[]>([])
  const traceEvents = ref<SpanEvent[]>([])
  const traceDetailLoading = ref(false)
  const traceDetailError = ref<string | null>(null)

  const selectedSpanId = ref<string | null>(null)
  const spanDetail = ref<SpanDetail | null>(null)
  const spanDetailLoading = ref(false)
  const spanDetailError = ref<string | null>(null)

  // 单调递增令牌：旧请求返回时不得覆盖更新的结果。各域独立递增。
  let overviewToken = 0
  let tracesToken = 0
  let traceDetailToken = 0
  let spanDetailToken = 0

  async function loadOverview(query?: AnalyticsQuery): Promise<void> {
    const token = ++overviewToken
    loading.value = true
    error.value = null

    try {
      const result = await getAnalyticsOverview(query)
      if (token !== overviewToken) {
        return
      }
      overview.value = result
    } catch (cause) {
      if (token !== overviewToken) {
        return
      }
      overview.value = null
      error.value = errorToMessage(cause)
    } finally {
      if (token === overviewToken) {
        loading.value = false
      }
    }
  }

  /** 按 URL 解析出的参数加载 Trace 列表；失败时清空旧数据，避免展示过期结果。 */
  async function loadTraces(params: ListTracesParams = {}): Promise<void> {
    const token = ++tracesToken
    tracesLoading.value = true
    tracesError.value = null

    try {
      const result = await listTraces(params)
      if (token !== tracesToken) {
        return
      }
      traces.value = result.items
      tracesTotal.value = result.total
    } catch (cause) {
      if (token !== tracesToken) {
        return
      }
      traces.value = []
      tracesTotal.value = 0
      tracesError.value = errorToMessage(cause)
    } finally {
      if (token === tracesToken) {
        tracesLoading.value = false
      }
    }
  }

  /**
   * 并行加载 Trace 概要、Span 树、扁平 Span 与事件。
   * 任一请求失败都视为整页失败：清空旧数据，避免混入上一次 Trace 的结果。
   */
  async function loadTraceDetail(traceId: string): Promise<void> {
    const token = ++traceDetailToken
    // 新 Trace 开始时让旧的 Span 详情请求失效。
    spanDetailToken += 1
    traceDetailLoading.value = true
    traceDetailError.value = null
    selectedSpanId.value = null
    spanDetail.value = null
    spanDetailLoading.value = false
    spanDetailError.value = null

    try {
      const [detail, tree, spans, events] = await Promise.all([
        getTrace(traceId),
        getTraceTree(traceId),
        listTraceSpans(traceId),
        listTraceEvents(traceId),
      ])
      if (token !== traceDetailToken) {
        return
      }
      traceDetail.value = detail
      traceTree.value = tree
      traceSpans.value = spans
      traceEvents.value = events
    } catch (cause) {
      if (token !== traceDetailToken) {
        return
      }
      traceDetail.value = null
      traceTree.value = []
      traceSpans.value = []
      traceEvents.value = []
      traceDetailError.value = errorToMessage(cause)
    } finally {
      if (token === traceDetailToken) {
        traceDetailLoading.value = false
      }
    }
  }

  /** 选中 Span 并懒加载详情；快速切换时旧响应不会覆盖新选择。 */
  async function selectTraceSpan(spanId: string): Promise<void> {
    const token = ++spanDetailToken
    selectedSpanId.value = spanId
    spanDetail.value = null
    spanDetailLoading.value = true
    spanDetailError.value = null

    try {
      const detail = await getSpan(spanId)
      // 期间已切到别的 Span（或整页重载）时丢弃这次过期响应。
      if (token !== spanDetailToken || selectedSpanId.value !== spanId) {
        return
      }
      spanDetail.value = detail
    } catch (cause) {
      if (token !== spanDetailToken || selectedSpanId.value !== spanId) {
        return
      }
      spanDetailError.value = errorToMessage(cause)
    } finally {
      if (token === spanDetailToken) {
        spanDetailLoading.value = false
      }
    }
  }

  /** 清空选中态与 Span 详情，并让在途的详情请求失效。 */
  function clearSelectedSpan(): void {
    spanDetailToken += 1
    selectedSpanId.value = null
    spanDetail.value = null
    spanDetailLoading.value = false
    spanDetailError.value = null
  }

  /** 清空状态并让任何在途请求失效。 */
  function reset(): void {
    overviewToken += 1
    overview.value = null
    loading.value = false
    error.value = null

    tracesToken += 1
    traces.value = []
    tracesTotal.value = 0
    tracesLoading.value = false
    tracesError.value = null

    traceDetailToken += 1
    spanDetailToken += 1
    traceDetail.value = null
    traceTree.value = []
    traceSpans.value = []
    traceEvents.value = []
    traceDetailLoading.value = false
    traceDetailError.value = null
    selectedSpanId.value = null
    spanDetail.value = null
    spanDetailLoading.value = false
    spanDetailError.value = null
  }

  return {
    overview,
    loading,
    error,
    loadOverview,
    traces,
    tracesTotal,
    tracesLoading,
    tracesError,
    loadTraces,
    traceDetail,
    traceTree,
    traceSpans,
    traceEvents,
    traceDetailLoading,
    traceDetailError,
    selectedSpanId,
    spanDetail,
    spanDetailLoading,
    spanDetailError,
    loadTraceDetail,
    selectTraceSpan,
    clearSelectedSpan,
    reset,
  }
})

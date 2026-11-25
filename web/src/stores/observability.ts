import { ref } from 'vue'
import { defineStore } from 'pinia'

import { ApiError } from '@/api/http'
import {
  getAnalyticsOverview,
  listTraces,
  type AnalyticsOverview,
  type AnalyticsQuery,
  type ListTracesParams,
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

  // 单调递增令牌：旧请求返回时不得覆盖更新的结果。两个域各自独立递增。
  let overviewToken = 0
  let tracesToken = 0

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
    reset,
  }
})

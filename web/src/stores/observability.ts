import { ref } from 'vue'
import { defineStore } from 'pinia'

import { ApiError } from '@/api/http'
import {
  getAnalyticsOverview,
  type AnalyticsOverview,
  type AnalyticsQuery,
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
 * 可观测分析状态仓库。
 * 当前只承载 Overview；后续 Task 15–17 可在此按域继续添加 traces、prompts 等状态。
 */
export const useObservabilityStore = defineStore('observability', () => {
  const overview = ref<AnalyticsOverview | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  // 单调递增令牌：旧请求返回时不得覆盖更新的结果。
  let loadToken = 0

  async function loadOverview(query?: AnalyticsQuery): Promise<void> {
    const token = ++loadToken
    loading.value = true
    error.value = null

    try {
      const result = await getAnalyticsOverview(query)
      if (token !== loadToken) {
        return
      }
      overview.value = result
    } catch (cause) {
      if (token !== loadToken) {
        return
      }
      overview.value = null
      error.value = errorToMessage(cause)
    } finally {
      if (token === loadToken) {
        loading.value = false
      }
    }
  }

  /** 清空状态并让任何在途请求失效。 */
  function reset(): void {
    loadToken += 1
    overview.value = null
    loading.value = false
    error.value = null
  }

  return {
    overview,
    loading,
    error,
    loadOverview,
    reset,
  }
})

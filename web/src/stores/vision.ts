import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { ApiError } from '@/api/http'
import {
  visionApi,
  type VisionEvent,
  type VisionFrame,
  type VisionResults,
  type VisionTaskDetail,
} from '@/api/vision'

/** 任务终态：命中后停止轮询，不再占用推理服务状态接口。 */
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled'])

/** 正常轮询间隔与网络异常退避上限。 */
const POLL_INTERVAL_MS = 2000
const MAX_BACKOFF_MS = 30_000

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
 * Vision 任务仓库：负责识别状态轮询与结果/事件缓存。
 *
 * 关键节点：
 * 1. `startPolling` 立即拉一次详情，之后 2s 递归 setTimeout；不使用
 *    setInterval，避免慢请求下定时器堆积；
 * 2. 网络异常按 4s → 8s → 16s → 30s 指数退避（封顶 30s），恢复后回到 2s；
 * 3. 每次轮询都校验单调令牌，旧响应和 `stopPolling` 之后的在途请求不得写回；
 * 4. `completed` 才读取 artifact 结果，`failed`/`cancelled` 停止且不请求结果。
 */
export const useVisionStore = defineStore('vision', () => {
  const task = ref<VisionTaskDetail | null>(null)
  const results = ref<VisionResults | null>(null)
  const events = ref<VisionEvent[]>([])
  const loading = ref(false)
  const error = ref<string | null>(null)
  const polling = ref(false)
  /** 结果读取失败原因（例如 404 artifact_missing）；任务详情仍可展示。 */
  const resultsError = ref<string | null>(null)
  const artifactMissing = ref(false)
  const eventsError = ref<string | null>(null)

  /** 按 artifact 原顺序返回帧；没有结果时为空数组，绝不补假帧。 */
  const frames = computed<VisionFrame[]>(() => results.value?.frames ?? [])

  let timer: ReturnType<typeof setTimeout> | null = null
  let backoff = POLL_INTERVAL_MS
  let pollToken = 0
  let detailToken = 0
  let resultsToken = 0

  function clearTimer(): void {
    if (timer !== null) {
      clearTimeout(timer)
      timer = null
    }
  }

  /** 清空任务/结果缓存，让旧任务的可见数据不会串到新任务。 */
  function resetTaskState(): void {
    task.value = null
    results.value = null
    resultsError.value = null
    artifactMissing.value = false
    events.value = []
    eventsError.value = null
  }

  /**
   * 读取任务详情（单次，不启动轮询）。
   *
   * @param taskUid 目标任务 UID。
   * @returns 最新的任务详情；失败时返回 null。
   */
  async function loadTask(taskUid: string): Promise<VisionTaskDetail | null> {
    const token = ++detailToken
    loading.value = true
    error.value = null

    try {
      const detail = await visionApi.task(taskUid)
      if (token !== detailToken) {
        return null
      }
      task.value = detail
      return detail
    } catch (cause) {
      if (token !== detailToken) {
        return null
      }
      task.value = null
      error.value = errorToMessage(cause)
      return null
    } finally {
      if (token === detailToken) {
        loading.value = false
      }
    }
  }

  /**
   * 读取 artifact 帧级结果；失败原因保留在 resultsError。
   *
   * 404 表示结果文件已清理（artifact_missing），页面据此提示且不渲染播放器。
   *
   * @param taskUid 目标任务 UID。
   */
  async function loadResults(taskUid: string): Promise<void> {
    const token = ++resultsToken
    try {
      const data = await visionApi.results(taskUid)
      if (token !== resultsToken) {
        return
      }
      results.value = data
      resultsError.value = null
      artifactMissing.value = false
    } catch (cause) {
      if (token !== resultsToken) {
        return
      }
      results.value = null
      resultsError.value = errorToMessage(cause)
      artifactMissing.value = cause instanceof ApiError && cause.status === 404
    }
  }

  /**
   * 分页读取任务事件（默认 20 条）。失败不阻断任务详情的其他区域。
   *
   * @param taskUid 目标任务 UID。
   */
  async function loadEvents(taskUid: string): Promise<void> {
    eventsError.value = null
    try {
      const response = await visionApi.events(taskUid, { page: 1, size: 20 })
      events.value = Array.isArray(response)
        ? response
        : (response?.items ?? [])
    } catch (cause) {
      events.value = []
      eventsError.value = errorToMessage(cause)
    }
  }

  /**
   * 轮询单次：成功写回详情，终态停止；失败退避后继续。
   *
   * @param taskUid 目标任务 UID。
   * @param token 启动轮询时捕获的令牌，用于丢弃过期响应。
   */
  async function poll(taskUid: string, token: number): Promise<void> {
    try {
      const detail = await visionApi.task(taskUid)
      if (token !== pollToken) {
        return
      }
      task.value = detail
      loading.value = false
      error.value = null
      backoff = POLL_INTERVAL_MS

      if (TERMINAL_STATUSES.has(detail.status)) {
        // 终态：让在途响应与后续调度一起失效。
        pollToken += 1
        clearTimer()
        polling.value = false
        if (detail.status === 'completed') {
          await loadResults(taskUid)
        }
        return
      }
    } catch (cause) {
      if (token !== pollToken) {
        return
      }
      loading.value = false
      error.value = errorToMessage(cause)
      backoff = Math.min(backoff * 2, MAX_BACKOFF_MS)
    }

    if (token !== pollToken) {
      return
    }
    timer = setTimeout(() => {
      void poll(taskUid, token)
    }, backoff)
  }

  /**
   * 开始轮询任务状态：立即请求一次，非终态时每 2s 递归一次。
   *
   * @param taskUid 目标任务 UID。
   */
  function startPolling(taskUid: string): void {
    stopPolling()
    resetTaskState()

    pollToken += 1
    const token = pollToken
    backoff = POLL_INTERVAL_MS
    polling.value = true
    loading.value = true
    error.value = null
    void poll(taskUid, token)
  }

  /** 停止轮询并清除定时器；在途响应会被令牌丢弃。 */
  function stopPolling(): void {
    pollToken += 1
    clearTimer()
    polling.value = false
    loading.value = false
  }

  /** 清空全部状态并让在途请求失效。 */
  function reset(): void {
    stopPolling()
    detailToken += 1
    resultsToken += 1
    resetTaskState()
    error.value = null
  }

  return {
    task,
    results,
    frames,
    events,
    loading,
    error,
    polling,
    resultsError,
    artifactMissing,
    eventsError,
    startPolling,
    stopPolling,
    loadTask,
    loadResults,
    loadEvents,
    reset,
  }
})

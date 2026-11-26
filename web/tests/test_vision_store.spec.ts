import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/api/vision', () => ({
  visionApi: {
    task: vi.fn(),
    results: vi.fn(),
  },
}))

import { visionApi } from '@/api/vision'
import { useVisionStore } from '@/stores/vision'

function running(uid = 'task-1') {
  return { task_uid: uid, status: 'running', progress: 10 }
}

function completed(uid = 'task-1') {
  return { task_uid: uid, status: 'completed', progress: 100 }
}

describe('vision store 轮询（Task 20）', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    vi.clearAllMocks()
    vi.mocked(visionApi.results).mockResolvedValue({ frames: [] } as never)
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('正常 2s 轮询，任务完成后停止并加载结果', async () => {
    vi.mocked(visionApi.task)
      .mockResolvedValueOnce(running() as never)
      .mockResolvedValueOnce(completed() as never)

    const store = useVisionStore()
    store.startPolling('task-1')
    await vi.advanceTimersByTimeAsync(0)

    expect(visionApi.task).toHaveBeenCalledTimes(1)
    expect(store.polling).toBe(true)

    await vi.advanceTimersByTimeAsync(1999)
    expect(visionApi.task).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(visionApi.task).toHaveBeenCalledTimes(2)

    expect(store.polling).toBe(false)
    expect(visionApi.results).toHaveBeenCalledWith('task-1')
  })

  it('网络异常按 4s→8s→16s→30s 退避并封顶', async () => {
    vi.mocked(visionApi.task).mockRejectedValue(new Error('network down'))

    const store = useVisionStore()
    store.startPolling('task-1')
    await vi.advanceTimersByTimeAsync(0)
    expect(visionApi.task).toHaveBeenCalledTimes(1)

    // 第一次失败后不得在 2s 立刻重试（退避到 4s）。
    await vi.advanceTimersByTimeAsync(2000)
    expect(visionApi.task).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(2000)
    expect(visionApi.task).toHaveBeenCalledTimes(2)

    // 之后 8s、16s，最终封顶 30s。
    await vi.advanceTimersByTimeAsync(7999)
    expect(visionApi.task).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(visionApi.task).toHaveBeenCalledTimes(3)

    await vi.advanceTimersByTimeAsync(15999)
    expect(visionApi.task).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(1)
    expect(visionApi.task).toHaveBeenCalledTimes(4)

    await vi.advanceTimersByTimeAsync(29999)
    expect(visionApi.task).toHaveBeenCalledTimes(4)
    await vi.advanceTimersByTimeAsync(1)
    expect(visionApi.task).toHaveBeenCalledTimes(5)

    // 已经封顶：再等 30s 才允许下一次。
    await vi.advanceTimersByTimeAsync(29999)
    expect(visionApi.task).toHaveBeenCalledTimes(5)
    await vi.advanceTimersByTimeAsync(1)
    expect(visionApi.task).toHaveBeenCalledTimes(6)
    expect(store.polling).toBe(true)
  })

  it('失败终态立即停止且不加载结果', async () => {
    vi.mocked(visionApi.task).mockResolvedValue({
      task_uid: 'task-1',
      status: 'failed',
      error_code: 'inference_failed',
    } as never)

    const store = useVisionStore()
    store.startPolling('task-1')
    await vi.advanceTimersByTimeAsync(0)

    expect(store.polling).toBe(false)
    expect(visionApi.results).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(2000)
    expect(visionApi.task).toHaveBeenCalledTimes(1)
  })

  it('stopPolling 清除定时器，不再发请求', async () => {
    vi.mocked(visionApi.task).mockResolvedValue(running() as never)

    const store = useVisionStore()
    store.startPolling('task-1')
    await vi.advanceTimersByTimeAsync(0)
    expect(visionApi.task).toHaveBeenCalledTimes(1)

    store.stopPolling()
    expect(store.polling).toBe(false)
    await vi.advanceTimersByTimeAsync(60_000)
    expect(visionApi.task).toHaveBeenCalledTimes(1)
  })
})

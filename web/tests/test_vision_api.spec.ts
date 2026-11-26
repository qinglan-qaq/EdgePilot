import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/api/http', () => ({
  api: {
    get: vi.fn(),
    post: vi.fn(),
  },
}))

import { api } from '@/api/http'
import { visionApi } from '@/api/vision'

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(api.get).mockResolvedValue({} as never)
  vi.mocked(api.post).mockResolvedValue({} as never)
})

function firstGet() {
  return vi.mocked(api.get).mock.calls[0]
}

function firstPost() {
  return vi.mocked(api.post).mock.calls[0]
}

describe('visionApi 路由映射（Task 20）', () => {
  it('只读接口映射到 14 路由中的 GET 部分', async () => {
    await visionApi.health()
    expect(firstGet()).toEqual(['/vision/health'])
    vi.mocked(api.get).mockClear()

    await visionApi.overview()
    expect(firstGet()).toEqual(['/vision/overview'])
    vi.mocked(api.get).mockClear()

    await visionApi.models()
    expect(firstGet()).toEqual(['/vision/models'])
    vi.mocked(api.get).mockClear()

    await visionApi.tasks({ page: 2, size: 20, status: 'failed', q: 'gate' })
    expect(firstGet()?.[0]).toBe('/vision/tasks')
    expect(firstGet()?.[1]).toEqual({
      params: { page: 2, size: 20, status: 'failed', q: 'gate' },
    })
    vi.mocked(api.get).mockClear()

    await visionApi.task('task-1')
    expect(firstGet()).toEqual(['/vision/tasks/task-1'])
    vi.mocked(api.get).mockClear()

    await visionApi.results('task-1', { class_name: 'person', min_confidence: 0.5 })
    expect(firstGet()?.[0]).toBe('/vision/tasks/task-1/results')
    expect(firstGet()?.[1]).toEqual({
      params: { class_name: 'person', min_confidence: 0.5 },
    })
    vi.mocked(api.get).mockClear()

    await visionApi.events('task-1', { page: 1, size: 20 })
    expect(firstGet()?.[0]).toBe('/vision/tasks/task-1/events')
    expect(firstGet()?.[1]).toEqual({ params: { page: 1, size: 20 } })
  })

  it('写操作映射到 cancel/retry/trigger/analyze/analyzeEvent', async () => {
    await visionApi.cancel('task-1')
    expect(firstPost()?.[0]).toBe('/vision/tasks/task-1/cancel')
    vi.mocked(api.post).mockClear()

    await visionApi.retry('task-1')
    expect(firstPost()?.[0]).toBe('/vision/tasks/task-1/retry')
    vi.mocked(api.post).mockClear()

    await visionApi.trigger('task-1')
    expect(firstPost()?.[0]).toBe('/vision/tasks/task-1/trigger')
    vi.mocked(api.post).mockClear()

    await visionApi.analyze('task-1')
    expect(firstPost()?.[0]).toBe('/vision/tasks/task-1/analyze')
    vi.mocked(api.post).mockClear()

    await visionApi.analyzeEvent(7)
    expect(firstPost()?.[0]).toBe('/vision/events/7/analyze')
  })

  it('上传以 multipart FormData POST 到 /vision/tasks', async () => {
    const form = new FormData()
    form.append('file', new File([new Uint8Array(4)], 'gate.mp4', { type: 'video/mp4' }))

    await visionApi.createTask(form)

    const call = firstPost()
    expect(call?.[0]).toBe('/vision/tasks')
    expect(call?.[1]).toBe(form)
  })
})

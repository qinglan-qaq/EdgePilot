import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter, type Router } from 'vue-router'
import type { Component } from 'vue'

import type { UserRole } from '../src/api/http'

// auth 仓库只依赖 role，保持 mock 轻量；避免页面在挂载时读取真实用户信息。
const authState = vi.hoisted(() => ({ role: 'operator' as UserRole }))

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => authState,
}))

vi.mock('@/api/vision', () => ({
  visionApi: {
    health: vi.fn(),
    overview: vi.fn(),
    models: vi.fn(),
    tasks: vi.fn(),
    createTask: vi.fn(),
    task: vi.fn(),
    results: vi.fn(),
    cancel: vi.fn(),
    retry: vi.fn(),
    trigger: vi.fn(),
    events: vi.fn(),
    analyze: vi.fn(),
    analyzeEvent: vi.fn(),
  },
}))

import { ApiError } from '../src/api/http'
import { visionApi } from '../src/api/vision'
import AsyncState from '../src/components/common/AsyncState.vue'
import { canAccessRoles } from '../src/router/access'
import router from '../src/router'
import Detect from '../src/views/vision/Detect.vue'
import Models from '../src/views/vision/Models.vue'
import Overview from '../src/views/vision/Overview.vue'
import Records from '../src/views/vision/Records.vue'
import TaskDetail from '../src/views/vision/TaskDetail.vue'

enableAutoUnmount(afterEach)

interface TaskRowFixture {
  task_uid: string
  video_filename: string
  status: string
  progress: number
  video_duration_ms: number | null
  processed_frames: number | null
  detection_count: number
  event_count: number
  model_version: string | null
  error_code: string | null
  created_at: string
  finished_at: string | null
}

function makeTaskRow(overrides: Partial<TaskRowFixture> = {}): TaskRowFixture {
  return {
    task_uid: 'task-1',
    video_filename: 'gate.mp4',
    status: 'completed',
    progress: 100,
    video_duration_ms: 20_000,
    processed_frames: 100,
    detection_count: 12,
    event_count: 1,
    model_version: 'yolo11n-v1',
    error_code: null,
    created_at: '2026-11-26T10:00:00+00:00',
    finished_at: '2026-11-26T10:00:30+00:00',
    ...overrides,
  }
}

function makeTasksResponse(items: TaskRowFixture[] = [], total = items.length) {
  return { items, total, page: 1, size: 20 }
}

const TASK_RESULT = {
  task_uid: 'task-1',
  agent_event_id: 9,
  frame_count: 100,
  detection_count: 12,
  class_counts: { person: 12 },
  peak_confidence: 0.93,
  inference_ms: 4200,
  event_count: 1,
  summary: {
    processed_frames: 100,
    detection_count: 12,
    class_counts: { person: 12 },
    peak_confidence: 0.93,
    inference_ms: 4200,
  },
  workflow_run_id: 88,
  trigger_status: 'triggered',
  trigger_error: null,
  agent_run_id: 901,
  agent_status: 'succeeded',
  agent_error: null,
  agent_trace_id: 'trace-1',
  artifact_path: null,
  artifact_bytes: 512,
  artifact_file_sha256: 'a'.repeat(64),
  result_sha256: 'b'.repeat(64),
  result_schema_version: 'vision-result/v1',
  purged_at: null,
}

// 详情 = VisionTaskOut + 媒体状态 + result（plan Task 8/16）；
// 列表用的轻量字段与播放器数据分别来自 tasks/results 接口。
const TASK_DETAIL = {
  id: 1,
  task_uid: 'task-1',
  video_filename: 'gate.mp4',
  status: 'completed',
  progress: 100,
  file_size_bytes: 2048,
  conf_threshold: 0.25,
  frame_stride: 5,
  target_samples: 900,
  video_fps: 25,
  video_duration_ms: 20_000,
  video_width: 640,
  video_height: 480,
  frame_count: 500,
  sampled_every_n_frames: 5,
  model_name: 'yolo11n',
  model_version: 'yolo11n-v1',
  device: 'cpu',
  processed_frames: 100,
  detection_count: 12,
  inference_ms: 4200,
  event_count: 1,
  has_video: true,
  video_missing: false,
  artifact_purged: false,
  video_url: '/api/v1/vision/tasks/task-1/video?token=test&expires=1',
  video_expires_at: '2026-11-26T11:00:00+00:00',
  result: TASK_RESULT,
  error_code: null,
  error_message: null,
  created_at: '2026-11-26T10:00:00+00:00',
  started_at: '2026-11-26T10:00:01+00:00',
  finished_at: '2026-11-26T10:00:30+00:00',
}

const TASK_RESULTS = {
  task_uid: 'task-1',
  video: {
    width: 640,
    height: 480,
    fps: 25,
    duration_ms: 20_000,
    sampled_every_n_frames: 5,
  },
  model: { model_name: 'yolo11n', model_version: 'yolo11n-v1' },
  summary: {
    processed_frames: 100,
    detection_count: 12,
    class_counts: { person: 12 },
    peak_confidence: 0.93,
  },
  query: {
    start_ms: 0,
    end_ms: 20_000,
    class_name: null,
    min_confidence: 0.25,
    total: 1,
  },
  frames: [
    {
      frame_id: 0,
      timestamp_ms: 0,
      inference_ms: 42,
      detections: [
        {
          detection_id: '0-0',
          class_id: 0,
          class_name: 'person',
          confidence: 0.93,
          bbox: { x1: 10, y1: 10, x2: 50, y2: 90 },
        },
      ],
    },
  ],
}

const HEALTH = {
  enabled: true,
  reachable: true,
  service: { status: 'ok', version: '0.1.0' },
  model: {
    status: 'ready',
    model_name: 'yolo11n',
    model_version: 'yolo11n-v1',
    device: 'cpu',
    license: 'AGPL-3.0',
    load_error: null,
  },
  error: null,
}

const OVERVIEW = {
  tasks: { total: 2, completed: 1, running: 1, failed: 0 },
  frames: { processed_total: 100 },
  detections: { total: 12 },
  events: { total: 1, unacked: 1 },
  model: { status: 'ready', model_version: 'yolo11n-v1', device: 'cpu' },
  recent_tasks: [
    {
      task_uid: 'task-1',
      video_filename: 'gate.mp4',
      status: 'completed',
      detection_count: 12,
      created_at: '2026-11-26T10:00:00+00:00',
    },
  ],
}

// 模型接口的返回形状在 spec 里未固定；同时提供数组语义与 items/models 字段，
// 避免页面在实现阶段选择任一读取方式时把挂载测试误伤。
const MODELS_RESPONSE = Object.assign([], {
  items: [],
  models: [],
  reachable: true,
  health: HEALTH,
})

const EVENT_ROW = {
  id: 7,
  event_type: 'vision.detection',
  severity: 'info',
  confidence: 0.93,
  occurred_at: '2026-11-26T10:00:05+00:00',
  bbox: { x: 10, y: 10, w: 40, h: 80 },
  payload: { task_uid: 'task-1', class_name: 'person' },
  dedup_key: 'dedup-1',
}

const EVENTS_RESPONSE = Object.assign([EVENT_ROW], {
  items: [EVENT_ROW],
  total: 1,
  page: 1,
  size: 20,
})

function makeTestRouter(): Router {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      { path: '/vision', component: { template: '<div />' } },
      { path: '/vision/detect', component: { template: '<div />' } },
      { path: '/vision/records', component: { template: '<div />' } },
      { path: '/vision/records/:taskUid', component: { template: '<div />' } },
      { path: '/vision/models', component: { template: '<div />' } },
    ],
  })
}

async function mountPage(component: Component, path: string) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const testRouter = makeTestRouter()
  await testRouter.push(path)
  await testRouter.isReady()

  const wrapper = mount(component, {
    attachTo: document.body,
    global: {
      plugins: [ElementPlus, pinia, testRouter],
    },
  })
  await flushPromises()
  return wrapper
}

type PageWrapper = Awaited<ReturnType<typeof mountPage>>

function findButtonByText(wrapper: PageWrapper, text: string) {
  return wrapper.findAll('button').find((button) => button.text().trim() === text)
}

const PAGES: Array<{ name: string; component: Component; path: string }> = [
  { name: 'Overview', component: Overview, path: '/vision' },
  { name: 'Detect', component: Detect, path: '/vision/detect' },
  { name: 'Records', component: Records, path: '/vision/records' },
  { name: 'TaskDetail', component: TaskDetail, path: '/vision/records/task-1' },
  { name: 'Models', component: Models, path: '/vision/models' },
]

beforeEach(() => {
  vi.clearAllMocks()
  authState.role = 'operator'

  vi.mocked(visionApi.tasks).mockResolvedValue(makeTasksResponse() as never)
  vi.mocked(visionApi.task).mockResolvedValue(TASK_DETAIL as never)
  vi.mocked(visionApi.results).mockResolvedValue(TASK_RESULTS as never)
  vi.mocked(visionApi.events).mockResolvedValue(EVENTS_RESPONSE as never)
  vi.mocked(visionApi.overview).mockResolvedValue(OVERVIEW as never)
  vi.mocked(visionApi.health).mockResolvedValue(HEALTH as never)
  vi.mocked(visionApi.models).mockResolvedValue(MODELS_RESPONSE as never)
  vi.mocked(visionApi.createTask).mockResolvedValue({ task: TASK_DETAIL, deduplicated: false } as never)
  vi.mocked(visionApi.cancel).mockResolvedValue(TASK_DETAIL as never)
  vi.mocked(visionApi.retry).mockResolvedValue(TASK_DETAIL as never)
  vi.mocked(visionApi.trigger).mockResolvedValue({} as never)
  vi.mocked(visionApi.analyze).mockResolvedValue({} as never)
  vi.mocked(visionApi.analyzeEvent).mockResolvedValue({} as never)

  // 页面如果挂载播放器，jsdom 不实现 canvas 与 rAF；这里提供最小桩，
  // 让页面级测试聚焦路由/角色/空态，而不是重复播放器绘制细节。
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({
    clearRect: vi.fn(),
    strokeRect: vi.fn(),
    fillRect: vi.fn(),
    fillText: vi.fn(),
    beginPath: vi.fn(),
    closePath: vi.fn(),
    stroke: vi.fn(),
    fill: vi.fn(),
    save: vi.fn(),
    restore: vi.fn(),
    measureText: vi.fn(() => ({ width: 40 })),
  } as never)
  vi.stubGlobal('requestAnimationFrame', vi.fn(() => 0))
  vi.stubGlobal('cancelAnimationFrame', vi.fn())
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('vision 路由与导航元数据', () => {
  it('/vision 是 nav 分组，五个子路由齐全且详情页不进入导航', () => {
    const rootRoute = router.options.routes.find((route) => route.path === '/')
    const visionRoute = rootRoute?.children?.find(
      (route) => route.path.replace(/^\/+/, '') === 'vision',
    )

    expect(visionRoute).toBeTruthy()
    expect(visionRoute?.name).toBe('vision')
    expect(visionRoute?.meta?.nav).toBe(true)
    expect(visionRoute?.meta?.icon).toBe('vision')

    const children = visionRoute?.children ?? []
    const byPath = new Map(
      children.map((child) => [child.path.replace(/^\/+/, ''), child]),
    )

    for (const path of ['', 'detect', 'records', 'records/:taskUid', 'models']) {
      expect(byPath.has(path)).toBe(true)
    }

    for (const path of ['', 'detect', 'records', 'models']) {
      const child = byPath.get(path)
      expect(child?.meta?.nav).toBe(true)
      expect(typeof child?.name).toBe('string')
      expect(String(child?.name ?? '').length).toBeGreaterThan(0)
    }

    const detail = byPath.get('records/:taskUid')
    expect(detail?.meta?.nav).toBe(false)
    expect(detail?.meta?.activeMenu).toBe('/vision/records')
  })

  it('所有角色都能读取 vision 分组与子路由，只有写操作按角色隐藏', () => {
    const rootRoute = router.options.routes.find((route) => route.path === '/')
    const visionRoute = rootRoute?.children?.find(
      (route) => route.path.replace(/^\/+/, '') === 'vision',
    )
    const children = visionRoute?.children ?? []

    for (const role of ['viewer', 'operator', 'admin'] as UserRole[]) {
      expect(canAccessRoles(visionRoute?.meta?.roles, role)).toBe(true)
      for (const child of children) {
        expect(canAccessRoles(child.meta?.roles, role)).toBe(true)
      }
    }
  })
})

describe('vision 页面骨架', () => {
  it('五个页面组件都可挂载，并统一使用 AsyncState 承载加载/错误/空态', async () => {
    for (const page of PAGES) {
      const wrapper = await mountPage(page.component, page.path)
      expect(wrapper.findComponent(AsyncState).exists()).toBe(true)
      wrapper.unmount()
    }
  })

  it('viewer 看不到视频上传入口，operator 可以看到', async () => {
    authState.role = 'viewer'
    const viewerWrapper = await mountPage(Detect, '/vision/detect')
    expect(viewerWrapper.find('[data-test="upload"]').exists()).toBe(false)
    viewerWrapper.unmount()

    authState.role = 'operator'
    const operatorWrapper = await mountPage(Detect, '/vision/detect')
    expect(operatorWrapper.find('[data-test="upload"]').exists()).toBe(true)
  })

  it('viewer 在任务详情页看不到取消/重试/研判按钮', async () => {
    authState.role = 'viewer'
    vi.mocked(visionApi.task).mockResolvedValue({
      ...TASK_DETAIL,
      status: 'failed',
      error_code: 'inference_failed',
      error_message: '推理失败',
    } as never)

    const wrapper = await mountPage(TaskDetail, '/vision/records/task-1')

    for (const label of ['取消', '重试', '研判']) {
      const visible = wrapper
        .findAll('button')
        .some((button) => button.text().includes(label))
      expect(visible).toBe(false)
    }
  })
})

describe('识别记录页', () => {
  it('默认按 20 条/页加载并渲染文件名与任务行', async () => {
    vi.mocked(visionApi.tasks).mockResolvedValue(
      makeTasksResponse([
        makeTaskRow(),
        makeTaskRow({
          task_uid: 'task-2',
          video_filename: 'parking.mp4',
          status: 'failed',
          progress: 40,
          detection_count: 3,
          event_count: 0,
        }),
      ]) as never,
    )

    const wrapper = await mountPage(Records, '/vision/records')

    expect(visionApi.tasks).toHaveBeenCalledTimes(1)
    expect(visionApi.tasks).toHaveBeenCalledWith(
      expect.objectContaining({ page: 1, size: 20 }),
    )
    const rows = wrapper.findAll('.el-table__row')
    expect(rows).toHaveLength(2)
    expect(rows[0]?.text()).toContain('gate.mp4')
    expect(rows[1]?.text()).toContain('parking.mp4')
  })

  it('空列表显示“暂无识别记录”，不渲染表格行', async () => {
    vi.mocked(visionApi.tasks).mockResolvedValue(makeTasksResponse() as never)

    const wrapper = await mountPage(Records, '/vision/records')

    expect(wrapper.find('[data-test="async-empty"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('暂无识别记录')
    expect(wrapper.findAll('.el-table__row')).toHaveLength(0)
  })

  it('加载失败显示后端错误，重试后渲染真实记录', async () => {
    vi.mocked(visionApi.tasks)
      .mockRejectedValueOnce(new ApiError(500, 500, '识别记录加载失败'))
      .mockResolvedValueOnce(makeTasksResponse([makeTaskRow()]) as never)

    const wrapper = await mountPage(Records, '/vision/records')

    expect(wrapper.find('[data-test="async-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('识别记录加载失败')

    const retryButton = findButtonByText(wrapper, '重试')
    expect(retryButton).toBeTruthy()
    await retryButton?.trigger('click')
    await flushPromises()

    expect(visionApi.tasks).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('gate.mp4')
    expect(wrapper.find('[data-test="async-content"]').exists()).toBe(true)
  })

  it('状态筛选回到第一页并携带 status，翻页按当前条件请求', async () => {
    vi.mocked(visionApi.tasks).mockResolvedValue(
      { items: [makeTaskRow()], total: 45, page: 1, size: 20 } as never,
    )

    const wrapper = await mountPage(Records, '/vision/records')

    const select = wrapper.findComponent({ name: 'ElSelect' })
    expect(select.exists()).toBe(true)
    select.vm.$emit('update:modelValue', 'failed')
    select.vm.$emit('change', 'failed')
    await flushPromises()

    expect(visionApi.tasks).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 1, size: 20, status: 'failed' }),
    )

    const page2 = wrapper.findAll('.el-pager li').find((item) => item.text() === '2')
    expect(page2).toBeTruthy()
    await page2?.trigger('click')
    await flushPromises()

    expect(visionApi.tasks).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 2, size: 20 }),
    )
  })

  it('文件名搜索 300ms 防抖，输入期间不立即请求', async () => {
    vi.mocked(visionApi.tasks).mockResolvedValue(makeTasksResponse() as never)

    const wrapper = await mountPage(Records, '/vision/records')
    expect(visionApi.tasks).toHaveBeenCalledTimes(1)

    const inputs = wrapper.findAll('input')
    const searchInput =
      inputs.find((input) => {
        const placeholder = input.attributes('placeholder') ?? ''
        return placeholder.includes('文件名') || placeholder.includes('搜索')
      }) ??
      inputs.find(
        (input) =>
          !input.attributes('readonly') &&
          (input.attributes('type') ?? 'text') === 'text',
      )
    expect(searchInput).toBeTruthy()

    await searchInput?.setValue('gate')
    await flushPromises()
    expect(visionApi.tasks).toHaveBeenCalledTimes(1)

    await new Promise((resolve) => setTimeout(resolve, 400))
    await flushPromises()

    expect(visionApi.tasks).toHaveBeenCalledTimes(2)
    expect(visionApi.tasks).toHaveBeenLastCalledWith(
      expect.objectContaining({ page: 1, size: 20, q: 'gate' }),
    )
  })
})

describe('任务详情页', () => {
  it('按路由参数加载任务并渲染文件名与 AsyncState 内容', async () => {
    const wrapper = await mountPage(TaskDetail, '/vision/records/task-1')

    expect(vi.mocked(visionApi.task).mock.calls[0]?.[0]).toBe('task-1')
    expect(wrapper.find('[data-test="async-content"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('gate.mp4')
  })
})


// ------------------------------------------------------------ Task 23 页面

const MODELS_WITH_ROW = Object.assign(
  [
    {
      model_key: 'yolo11n',
      model_name: 'YOLO11n',
      model_version: 'yolo11n-v1',
      provider: 'ultralytics',
      device: 'cpu',
      imgsz: 640,
      class_count: 80,
      license: 'AGPL-3.0',
      status: 'ready',
    },
  ],
  {
    items: [
      {
        model_key: 'yolo11n',
        model_name: 'YOLO11n',
        model_version: 'yolo11n-v1',
        provider: 'ultralytics',
        device: 'cpu',
        imgsz: 640,
        class_count: 80,
        license: 'AGPL-3.0',
        status: 'ready',
      },
    ],
    models: [],
    total: 1,
    reachable: true,
    health: HEALTH,
  },
)

describe('总览页（Task 23）', () => {
  it('渲染四张统计卡、最近任务与服务状态', async () => {
    vi.mocked(visionApi.overview).mockResolvedValue(OVERVIEW as never)
    vi.mocked(visionApi.health).mockResolvedValue(HEALTH as never)

    const wrapper = await mountPage(Overview, '/vision')

    expect(wrapper.find('[data-test="overview-stats"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-test="overview-stat"]')).toHaveLength(4)
    expect(wrapper.find('[data-test="overview-recent"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('gate.mp4')
    expect(wrapper.find('[data-test="vision-health"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('yolo11n-v1')
  })
})

describe('模型页（Task 23）', () => {
  it('常驻 AGPL 两条授权路径并展示 DB 模型行', async () => {
    vi.mocked(visionApi.models).mockResolvedValue(MODELS_WITH_ROW as never)
    vi.mocked(visionApi.health).mockResolvedValue(HEALTH as never)

    const wrapper = await mountPage(Models, '/vision/models')

    const alert = wrapper.find('[data-test="license-alert"]')
    expect(alert.exists()).toBe(true)
    const text = wrapper.text()
    expect(text).toContain('AGPL-3.0')
    expect(text).toContain('Enterprise License')
    expect(text).toContain('商业')
    expect(text).toContain('yolo11n-v1')
  })

  it('服务不可达时展示启动提示且保留 DB 模型行', async () => {
    vi.mocked(visionApi.models).mockResolvedValue(MODELS_WITH_ROW as never)
    vi.mocked(visionApi.health).mockResolvedValue({
      ...HEALTH,
      reachable: false,
      service: null,
      error: 'vision-service 不可达: ConnectError',
    } as never)

    const wrapper = await mountPage(Models, '/vision/models')
    const text = wrapper.text()

    expect(text).toContain('vision-service')
    expect(text).toMatch(/uvicorn|docker compose|启动/)
    expect(text).toContain('yolo11n-v1')
  })
})

describe('任务详情页（Task 23）', () => {
  it('渲染汇总、事件列表、Run/Trace 链接与 Agent 结果', async () => {
    const wrapper = await mountPage(TaskDetail, '/vision/records/task-1')
    await flushPromises()

    const text = wrapper.text()
    expect(text).toContain('person')
    expect(text).toContain('vision.detection')
    expect(text).toContain('triggered')
    expect(text).toContain('trace-1')
    expect(wrapper.find('a[href*="/runs/88"]').exists()).toBe(true)
  })

  it('operator 在失败任务上可重试与研判', async () => {
    const failedDetail = {
      ...TASK_DETAIL,
      status: 'failed',
      error_code: 'inference_failed',
      error_message: '推理失败',
      result: { ...TASK_RESULT, trigger_status: 'failed', agent_status: 'failed' },
    }
    vi.mocked(visionApi.task).mockResolvedValue(failedDetail as never)
    vi.mocked(visionApi.retry).mockResolvedValue(failedDetail as never)

    const wrapper = await mountPage(TaskDetail, '/vision/records/task-1')
    const retryButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('重试'))
    expect(retryButton).toBeTruthy()
    await retryButton?.trigger('click')
    await flushPromises()
    expect(visionApi.retry).toHaveBeenCalledWith('task-1')

    const analyzeButton = wrapper
      .findAll('button')
      .find((button) => button.text().includes('研判'))
    expect(analyzeButton).toBeTruthy()
    await analyzeButton?.trigger('click')
    await flushPromises()
    expect(visionApi.analyze).toHaveBeenCalledWith('task-1')
  })

  it('artifact 已清理时显示明确提示且不渲染播放器', async () => {
    vi.mocked(visionApi.task).mockResolvedValue({
      ...TASK_DETAIL,
      has_video: false,
      video_missing: true,
      artifact_purged: true,
      video_url: null,
      video_expires_at: null,
    } as never)
    vi.mocked(visionApi.results).mockRejectedValue(
      new ApiError(404, 404, '结果文件已清理'),
    )

    const wrapper = await mountPage(TaskDetail, '/vision/records/task-1')
    await flushPromises()

    expect(wrapper.text()).toContain('结果文件已清理')
    expect(wrapper.find('[data-test="vision-player"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('person 93.0%')
  })
})

describe('识别页上传（Task 22）', () => {
  it('上传合法视频后创建任务并进入轮询', async () => {
    vi.mocked(visionApi.createTask).mockResolvedValue({
      task: { ...TASK_DETAIL, status: 'running', progress: 0 },
      deduplicated: false,
    } as never)
    vi.mocked(visionApi.task).mockResolvedValue({
      ...TASK_DETAIL,
      status: 'running',
      progress: 0,
    } as never)

    const wrapper = await mountPage(Detect, '/vision/detect')
    const file = new File([new Uint8Array(32)], 'gate.mp4', { type: 'video/mp4' })
    const fileInput = wrapper.find('input[type="file"]')
    expect(fileInput.exists()).toBe(true)
    Object.defineProperty(fileInput.element, 'files', {
      value: [file],
      configurable: true,
    })
    await fileInput.trigger('change')

    const upload = wrapper.find('[data-test="upload"]')
    expect(upload.exists()).toBe(true)
    await upload.trigger('click')
    await flushPromises()
    await flushPromises()

    expect(visionApi.createTask).toHaveBeenCalledTimes(1)
    const payload = vi.mocked(visionApi.createTask).mock.calls[0]?.[0] as FormData
    expect(payload).toBeInstanceOf(FormData)
    expect(payload.get('file')).toBeTruthy()
    expect(visionApi.task).toHaveBeenCalledWith('task-1')
  })

  it('客户端拒绝非视频后缀且不提交请求', async () => {
    const wrapper = await mountPage(Detect, '/vision/detect')
    const file = new File([new Uint8Array(8)], 'notes.txt', { type: 'text/plain' })
    const fileInput = wrapper.find('input[type="file"]')
    Object.defineProperty(fileInput.element, 'files', {
      value: [file],
      configurable: true,
    })
    await fileInput.trigger('change')

    const upload = wrapper.find('[data-test="upload"]')
    await upload.trigger('click')
    await flushPromises()

    expect(visionApi.createTask).not.toHaveBeenCalled()
    expect(document.body.textContent ?? '').toMatch(/mp4|视频/)
  })
})

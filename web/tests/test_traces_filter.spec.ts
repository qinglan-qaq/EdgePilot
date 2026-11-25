import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'

import type { TraceSummary } from '../src/api/observability'

vi.mock('@/api/observability', () => ({
  getAnalyticsOverview: vi.fn(),
  listTraces: vi.fn(),
}))

import { listTraces } from '../src/api/observability'
import FilterBar from '../src/components/observability/FilterBar.vue'
import Traces from '../src/views/observability/Traces.vue'
import {
  buildTraceListParams,
  EMPTY_TRACE_FILTERS,
  parseTraceFilters,
  parseTracePage,
  type TraceFilterState,
} from '../src/utils/traceFilters'

enableAutoUnmount(afterEach)

const DetailStub = defineComponent({
  name: 'TraceDetailStub',
  render: () => h('div', { 'data-test': 'trace-detail-stub' }),
})

function makeTrace(index: number): TraceSummary {
  return {
    trace_id: `trace-${String(index).padStart(3, '0')}`,
    name: `巡检 Trace ${index}`,
    source: 'workflow',
    status: index % 2 === 0 ? 'failed' : 'succeeded',
    start_time: '2026-11-25T10:00:00+00:00',
    end_time: '2026-11-25T10:00:01+00:00',
    duration_ms: 1200 + index,
    total_spans: 3,
    error_count: index % 2 === 0 ? 1 : 0,
    total_tokens: index % 3 === 0 ? null : 100 + index,
    total_cost: index % 4 === 0 ? null : 0.001234,
  }
}

function createTestRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/observability/traces',
        name: 'observability-traces',
        component: Traces,
      },
      {
        path: '/observability/traces/:traceId',
        name: 'observability-trace-detail',
        component: DetailStub,
      },
    ],
  })
}

async function mountPage(initialPath = '/observability/traces') {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = createTestRouter()
  await router.push(initialPath)
  await router.isReady()

  const wrapper = mount(Traces, {
    attachTo: document.body,
    global: {
      plugins: [ElementPlus, pinia, router],
    },
  })
  await flushPromises()
  return { wrapper, router }
}

function lastParams() {
  return vi.mocked(listTraces).mock.calls.at(-1)?.[0] ?? {}
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listTraces).mockResolvedValue({
    items: [],
    total: 0,
    page: 1,
    size: 100,
  })
})

describe('Trace Explorer 列表', () => {
  it('刷新时从 URL 还原全部筛选，并按 page=2 / size=100 请求', async () => {
    const startFrom = '2026-11-01T00:00:00.000Z'
    const startTo = '2026-11-02T00:00:00.000Z'
    const query = new URLSearchParams({
      status: 'failed',
      trace_id: 'abc123',
      source: 'workflow',
      user_id: 'user-1',
      agent_id: '7',
      model: 'gpt-4o',
      tool: 'camera.snapshot',
      start_from: startFrom,
      start_to: startTo,
      min_duration_ms: '1500',
      failed: '1',
      slow: '1',
      page: '2',
    }).toString()

    vi.mocked(listTraces).mockResolvedValue({
      items: [makeTrace(1)],
      total: 101,
      page: 2,
      size: 100,
    })

    await mountPage(`/observability/traces?${query}`)

    expect(listTraces).toHaveBeenCalledTimes(1)
    expect(lastParams()).toEqual({
      page: 2,
      size: 100,
      status: 'failed',
      trace_id: 'abc123',
      source: 'workflow',
      user_id: 'user-1',
      agent_id: 7,
      model: 'gpt-4o',
      tool: 'camera.snapshot',
      start_from: startFrom,
      start_to: startTo,
      min_duration_ms: 1500,
      failed: true,
      slow: true,
    })
  })

  it('URL query 变化（应用筛选）会发起新的正确请求', async () => {
    vi.mocked(listTraces).mockResolvedValue({
      items: [makeTrace(1)],
      total: 1,
      page: 1,
      size: 100,
    })

    const { router } = await mountPage()
    expect(lastParams()).toEqual({ page: 1, size: 100 })

    await router.replace({
      query: { source: 'playground', failed: '1', page: '2' },
    })
    await flushPromises()

    expect(lastParams()).toEqual({
      page: 2,
      size: 100,
      source: 'playground',
      failed: true,
    })
  })

  it('100 行只渲染可见窗口，滚动后渲染后段行', async () => {
    const items = Array.from({ length: 100 }, (_, index) => makeTrace(index))
    vi.mocked(listTraces).mockResolvedValue({
      items,
      total: 100,
      page: 1,
      size: 100,
    })

    const { wrapper } = await mountPage()

    const initialRows = wrapper.findAll('[data-test="trace-row"]')
    expect(initialRows.length).toBeGreaterThan(0)
    expect(initialRows.length).toBeLessThan(100)
    expect(wrapper.text()).toContain('巡检 Trace 0')
    expect(wrapper.text()).not.toContain('巡检 Trace 95')

    const viewport = wrapper.find('[data-test="trace-table-viewport"]')
    const element = viewport.element as HTMLElement
    // 行高 44：滚到第 90 行附近，后段行应进入可见窗口。
    element.scrollTop = 90 * 44
    await viewport.trigger('scroll')
    await flushPromises()

    expect(wrapper.text()).toContain('巡检 Trace 95')
    expect(wrapper.findAll('[data-test="trace-row"]').length).toBeLessThan(100)
  })

  it('点击行跳转到 Trace 详情命名路由', async () => {
    vi.mocked(listTraces).mockResolvedValue({
      items: [makeTrace(7)],
      total: 1,
      page: 1,
      size: 100,
    })

    const { wrapper, router } = await mountPage()
    await wrapper.find('[data-test="trace-row"]').trigger('click')
    await flushPromises()

    expect(router.currentRoute.value.name).toBe('observability-trace-detail')
    expect(router.currentRoute.value.params.traceId).toBe('trace-007')
    expect(router.currentRoute.value.path).toBe('/observability/traces/trace-007')
  })

  it('无效数字不会进入请求参数（不会发送 NaN）', () => {
    const filters = parseTraceFilters({
      agent_id: 'not-a-number',
      min_duration_ms: 'NaN',
    })
    const page = parseTracePage({ page: 'two' })
    const params = buildTraceListParams(filters, page, 100)

    expect(params).toEqual({ page: 1, size: 100 })
    expect('agent_id' in params).toBe(false)
    expect('min_duration_ms' in params).toBe(false)
  })
})

describe('FilterBar', () => {
  it('修改 Trace ID / 来源并点击查询，emit 更新后的筛选与 apply', async () => {
    const wrapper = mount(FilterBar, {
      global: { plugins: [ElementPlus] },
      props: { modelValue: { ...EMPTY_TRACE_FILTERS } },
    })

    // Element Plus 的 el-input 会把透传属性绑定到内部 input 上，
    // 因此 data-test 可能直接落在 input，也可能落在根节点。
    const inputOf = (selector: string) => {
      const found = wrapper.find(selector)
      return found.element.tagName === 'INPUT' ? found : found.find('input')
    }

    await inputOf('[data-test="filter-trace-id"]').setValue('abc123')
    await inputOf('[data-test="filter-source"]').setValue('workflow')
    await wrapper.find('[data-test="filter-apply"]').trigger('click')

    const updates = wrapper.emitted('update:modelValue') ?? []
    const latest = updates.at(-1)?.[0] as TraceFilterState
    expect(latest.traceId).toBe('abc123')
    expect(latest.source).toBe('workflow')
    expect(wrapper.emitted('apply')).toHaveLength(1)
  })
})

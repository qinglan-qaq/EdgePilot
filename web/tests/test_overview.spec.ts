import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'

import type { AnalyticsOverview, OverviewCards, OverviewCharts } from '../src/api/observability'

vi.mock('@/api/observability', () => ({
  getAnalyticsOverview: vi.fn(),
}))

import { ApiError } from '../src/api/http'
import { getAnalyticsOverview } from '../src/api/observability'
import Overview from '../src/views/observability/Overview.vue'

enableAutoUnmount(afterEach)

const BASE_CARDS: OverviewCards = {
  traces_total: 12,
  trace_success_rate: 0.75,
  trace_p95_ms: 1500,
  spans_total: 48,
  agent_runs: 6,
  tool_success_rate: 0.9,
  tool_permission_denied: 2,
  llm_calls: 30,
  total_tokens: 12345,
  total_cost: 0.012345,
  cost_unavailable_count: 0,
  cost_coverage: { priced_models: 3, total_models: 3 },
}

const BASE_CHARTS: OverviewCharts = {
  trace_trend: [
    { bucket: '2026-11-25T00:00:00+00:00', total: 6, succeeded: 5, failed: 1 },
    { bucket: '2026-11-25T01:00:00+00:00', total: 6, succeeded: 4, failed: 2 },
  ],
  latency_trend: [
    { bucket: '2026-11-25T00:00:00+00:00', p50_ms: 800, p95_ms: 1500 },
    { bucket: '2026-11-25T01:00:00+00:00', p50_ms: null, p95_ms: 2400 },
  ],
  token_trend: [
    { bucket: '2026-11-25T00:00:00+00:00', total_tokens: 9000, cached_tokens: 1000 },
    { bucket: '2026-11-25T01:00:00+00:00', total_tokens: 3345, cached_tokens: null },
  ],
  model_distribution: [
    {
      provider: 'openai',
      model_name: 'gpt-4o',
      calls: 20,
      total_tokens: 9000,
      cost: 0.01,
      cost_unavailable_count: 0,
    },
  ],
  agent_distribution: [
    { label: '巡检 Agent', runs: 4, failed: 1, p95_ms: 1200 },
  ],
  tool_distribution: [
    { tool_id: 'camera.snapshot', tool_name: '抓帧', calls: 10, success_rate: 0.9, p95_ms: 300 },
  ],
  error_distribution: [{ code: 'timeout', count: 2 }],
  slowest_traces: [
    {
      trace_id: 'a'.repeat(32),
      name: '慢请求 A',
      status: 'failed',
      duration_ms: 2300,
      start_time: '2026-11-25T10:00:00+00:00',
    },
  ],
}

function makeOverview(
  overrides: { cards?: Partial<OverviewCards>; charts?: Partial<OverviewCharts> } = {},
): AnalyticsOverview {
  return {
    window: {
      start_from: '2026-11-25T00:00:00+00:00',
      start_to: '2026-11-26T00:00:00+00:00',
      bucket: 'hour',
    },
    cards: { ...BASE_CARDS, ...overrides.cards },
    charts: { ...BASE_CHARTS, ...overrides.charts },
  }
}

function emptyCharts(): Partial<OverviewCharts> {
  return {
    trace_trend: [],
    latency_trend: [],
    token_trend: [],
    model_distribution: [],
    agent_distribution: [],
    tool_distribution: [],
    error_distribution: [],
    slowest_traces: [],
  }
}

function mountPage() {
  const pinia = createPinia()
  setActivePinia(pinia)
  return mount(Overview, {
    attachTo: document.body,
    global: {
      plugins: [ElementPlus, pinia],
    },
  })
}

function findButton(wrapper: ReturnType<typeof mountPage>, text: string) {
  return wrapper.findAll('button').find((button) => button.text().trim() === text)
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('可观测概览页', () => {
  it('渲染 10 个指标卡，且每张卡都有非空口径提示', async () => {
    vi.mocked(getAnalyticsOverview).mockResolvedValue(makeOverview())

    const wrapper = mountPage()
    await flushPromises()

    const cards = wrapper.findAll('[data-test="metric-card"]')
    expect(cards).toHaveLength(10)

    const hints = wrapper.findAll('[data-test="metric-hint"]')
    expect(hints).toHaveLength(10)
    for (const hint of hints) {
      expect((hint.attributes('data-hint') ?? '').trim().length).toBeGreaterThan(0)
    }
  })

  it('成本与 token 缺失时显示未上报，并展示覆盖比例和缺价次数', async () => {
    vi.mocked(getAnalyticsOverview).mockResolvedValue(
      makeOverview({
        cards: {
          total_cost: null,
          total_tokens: null,
          cost_unavailable_count: 3,
          cost_coverage: { priced_models: 2, total_models: 3 },
        },
      }),
    )

    const wrapper = mountPage()
    await flushPromises()

    const costCard = wrapper.find('[data-metric="total_cost"]')
    expect(costCard.exists()).toBe(true)
    expect(costCard.text()).toContain('未上报')
    expect(costCard.text()).toContain('覆盖 2/3 模型')
    expect(costCard.text()).toContain('3 次调用缺价')

    const tokenCard = wrapper.find('[data-metric="total_tokens"]')
    expect(tokenCard.exists()).toBe(true)
    expect(tokenCard.text()).toContain('未上报')
    expect(tokenCard.text()).not.toContain('0')
  })

  it('有数据时渲染成功率、P95 耗时、最慢 trace 与 8 类图表', async () => {
    vi.mocked(getAnalyticsOverview).mockResolvedValue(makeOverview())

    const wrapper = mountPage()
    await flushPromises()

    // 默认近 24 小时不传 start_from/start_to，由后端解析
    expect(getAnalyticsOverview).toHaveBeenCalledTimes(1)
    expect(getAnalyticsOverview).toHaveBeenCalledWith({})

    const text = wrapper.text()
    expect(text).toContain('75.0%')
    expect(text).toContain('90.0%')
    expect(text).toContain('1.5 秒')
    expect(text).toContain('慢请求 A')
    expect(text).toContain('失败')
    expect(text).toContain('按小时分桶')

    expect(wrapper.find('[data-test="overview-window"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-test="trend-chart"]')).toHaveLength(3)
    expect(wrapper.findAll('[data-test="bar-list"]')).toHaveLength(4)

    // 成功率固定以 100% 为分母，不按当前最高工具归一化。
    const toolList = wrapper
      .findAll('[data-test="bar-list"]')
      .find((list) => list.text().includes('抓帧'))
    expect(toolList).toBeTruthy()
    const toolBar = toolList?.find('.bar-list__bar')
    expect(toolBar?.attributes('style')).toMatch(/width:\s*90(?:\.0)?%/)

    for (const title of [
      'Trace 趋势',
      '延迟趋势',
      'Token 趋势',
      '模型分布',
      'Agent 分布',
      '工具成功率',
      '错误分布',
      '最慢 Trace',
    ]) {
      expect(text).toContain(title)
    }
  })

  it('接口失败展示 AsyncState 错误，重试后渲染真实数据', async () => {
    vi.mocked(getAnalyticsOverview)
      .mockRejectedValueOnce(new ApiError(500, 500, '概览加载失败'))
      .mockResolvedValueOnce(makeOverview())

    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('[data-test="async-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('概览加载失败')

    const retryButton = findButton(wrapper, '重试')
    expect(retryButton).toBeTruthy()
    await retryButton?.trigger('click')
    await flushPromises()

    expect(getAnalyticsOverview).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-test="async-content"]').exists()).toBe(true)
    expect(wrapper.findAll('[data-test="metric-card"]')).toHaveLength(10)
  })

  it('图表为空时显示暂无数据，指标卡仍然渲染', async () => {
    vi.mocked(getAnalyticsOverview).mockResolvedValue(
      makeOverview({ charts: emptyCharts() }),
    )

    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.findAll('[data-test="metric-card"]')).toHaveLength(10)
    const emptyCount = wrapper.text().match(/暂无数据/g)?.length ?? 0
    expect(emptyCount).toBeGreaterThanOrEqual(8)
  })
})

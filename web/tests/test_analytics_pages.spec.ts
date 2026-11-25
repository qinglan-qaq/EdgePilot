import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { createMemoryHistory, createRouter } from 'vue-router'

vi.mock('@/api/observability', () => ({
  getAnalyticsAgents: vi.fn(),
  getAnalyticsModels: vi.fn(),
  getAnalyticsOverview: vi.fn(),
  getAnalyticsTools: vi.fn(),
  getObservabilitySettings: vi.fn(),
  getPromptTemplate: vi.fn(),
  getSpan: vi.fn(),
  getTrace: vi.fn(),
  getTraceTree: vi.fn(),
  listPromptExecutions: vi.fn(),
  listPromptTemplates: vi.fn(),
  listTraceEvents: vi.fn(),
  listTraceSpans: vi.fn(),
  listTraces: vi.fn(),
}))

import { getAnalyticsModels, type ModelAnalyticsItem } from '../src/api/observability'
import Models from '../src/views/observability/Models.vue'

enableAutoUnmount(afterEach)

const MODELS: ModelAnalyticsItem[] = [
  {
    provider: 'openai',
    model_name: 'gpt-4o',
    calls: 2,
    prompt_tokens: null,
    completion_tokens: null,
    cached_tokens: null,
    total_tokens: null,
    cost: null,
    cost_unavailable_count: 2,
    errors: 0,
  },
]

async function mountPage() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/', component: { template: '<div />' } },
      {
        path: '/observability/traces',
        name: 'observability-traces',
        component: { template: '<div />' },
      },
    ],
  })
  await router.push('/')
  await router.isReady()

  const wrapper = mount(Models, {
    global: { plugins: [ElementPlus, pinia, router] },
  })
  await flushPromises()
  return wrapper
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getAnalyticsModels).mockResolvedValue({ items: MODELS })
})

describe('Models 页面', () => {
  it('cost 为 null 时显示“费用不可用”，不用 0 冒充', async () => {
    const wrapper = await mountPage()

    // 真实行已渲染。
    expect(wrapper.text()).toContain('gpt-4o')
    expect(wrapper.find('[data-test="models-table"]').exists()).toBe(true)

    const costCell = wrapper.find('[data-test="model-cost"]')
    expect(costCell.exists()).toBe(true)
    expect(costCell.text()).toBe('费用不可用')

    // 汇总卡同样显示不可用，而不是 0。
    const costCard = wrapper.find('[data-metric="models_cost"]')
    expect(costCard.exists()).toBe(true)
    expect(costCard.text()).toContain('费用不可用')
    expect(costCard.find('.metric-card__value').text()).toBe('费用不可用')
  })
})

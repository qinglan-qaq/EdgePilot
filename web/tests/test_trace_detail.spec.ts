import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { defineComponent, h, nextTick } from 'vue'
import { createMemoryHistory, createRouter } from 'vue-router'

import type {
  SpanDetail,
  SpanEvent,
  SpanNode,
  TraceDetail as TraceDetailData,
} from '../src/api/observability'

vi.mock('@/api/observability', () => ({
  getTrace: vi.fn(),
  getTraceTree: vi.fn(),
  getSpan: vi.fn(),
  listTraceSpans: vi.fn(),
  listTraceEvents: vi.fn(),
  listTraces: vi.fn(),
  getAnalyticsOverview: vi.fn(),
}))

import { ApiError } from '../src/api/http'
import {
  getSpan,
  getTrace,
  getTraceTree,
  listTraceEvents,
  listTraceSpans,
} from '../src/api/observability'
import TraceDetail from '../src/views/observability/TraceDetail.vue'

enableAutoUnmount(afterEach)

const TRACE_ID = 'trace-1'

function leaf(
  spanId: string,
  parentSpanId: string | null,
  name: string,
  spanType: string,
  status: string,
  start: string,
  end: string,
  overrides: Partial<SpanNode> = {},
): SpanNode {
  return {
    span_id: spanId,
    parent_span_id: parentSpanId,
    name,
    span_type: spanType,
    status,
    attempt: 1,
    start_time: start,
    end_time: end,
    duration_ms: new Date(end).getTime() - new Date(start).getTime(),
    children: [],
    ...overrides,
  }
}

const SPAN_TREE: SpanNode[] = [
  leaf('span-1', null, 'workflow.run', 'workflow', 'succeeded', '2026-11-25T10:00:00.000Z', '2026-11-25T10:00:01.000Z', {
    children: [
      leaf('span-2', 'span-1', 'Agent 执行', 'agent', 'succeeded', '2026-11-25T10:00:00.100Z', '2026-11-25T10:00:00.900Z', {
        children: [
          leaf('span-3', 'span-2', '模型调用', 'llm', 'succeeded', '2026-11-25T10:00:00.200Z', '2026-11-25T10:00:00.600Z'),
          leaf('span-4', 'span-2', '工具调用失败', 'tool', 'failed', '2026-11-25T10:00:00.650Z', '2026-11-25T10:00:00.850Z', {
            attempt: 2,
            retry_count: 1,
            error: { code: 'timeout' },
            children: [
              leaf('span-5', 'span-4', '重试节点', 'node', 'succeeded', '2026-11-25T10:00:00.700Z', '2026-11-25T10:00:00.750Z'),
            ],
          }),
        ],
      }),
    ],
  }),
]

function flattenTree(nodes: SpanNode[]): SpanNode[] {
  const result: SpanNode[] = []
  const walk = (list: SpanNode[]): void => {
    for (const node of list) {
      const { children, ...rest } = node
      void children
      result.push(rest)
      if (node.children && node.children.length > 0) {
        walk(node.children)
      }
    }
  }
  walk(nodes)
  return result
}

const FLAT_SPANS = flattenTree(SPAN_TREE)

const EVENTS: SpanEvent[] = [
  {
    id: 3,
    trace_id: TRACE_ID,
    span_id: 'span-4',
    event_type: 'tool_error',
    event_data: { message: 'timeout' },
    sequence_number: 3,
    created_at: '2026-11-25T10:00:00.850Z',
  },
  {
    id: 1,
    trace_id: TRACE_ID,
    span_id: 'span-1',
    event_type: 'start',
    event_data: { step: 'start' },
    sequence_number: 1,
    created_at: '2026-11-25T10:00:00.000Z',
  },
  {
    id: 2,
    trace_id: TRACE_ID,
    span_id: 'span-3',
    event_type: 'llm_call',
    event_data: { model: 'qwen-plus' },
    sequence_number: 2,
    created_at: '2026-11-25T10:00:00.200Z',
  },
]

function makeTraceDetail(overrides: Partial<TraceDetailData> = {}): TraceDetailData {
  return {
    id: 1,
    trace_id: TRACE_ID,
    name: '设备巡检 Trace',
    source: 'workflow',
    user_id: 1,
    session_id: 'session-1',
    root_span_id: 'span-1',
    status: 'failed',
    input: null,
    output: null,
    start_time: '2026-11-25T10:00:00.000Z',
    end_time: '2026-11-25T10:00:01.000Z',
    duration_ms: 1000,
    total_tokens: 20,
    total_cost: null,
    cost_unavailable_count: 1,
    total_spans: 5,
    error_count: 1,
    meta: null,
    created_at: '2026-11-25T10:00:00.000Z',
    ...overrides,
  }
}

function makeSpanDetail(spanId: string): SpanDetail {
  const node = FLAT_SPANS.find((span) => span.span_id === spanId)
  if (!node) {
    throw new Error(`unknown span ${spanId}`)
  }
  const isLlm = spanId === 'span-3'
  const isFailedTool = spanId === 'span-4'
  return {
    ...node,
    input: isLlm ? { messages: ['设备是否在线'] } : null,
    output: isLlm ? { content: '设备在线' } : null,
    error: isFailedTool ? { code: 'timeout', message: '工具超时' } : null,
    llm_calls: isLlm
      ? [
          {
            id: 1,
            attempt: 1,
            provider: 'dashscope',
            model_name: 'qwen-plus',
            temperature: 0.2,
            top_p: 1,
            max_tokens: 512,
            prompt_tokens: 12,
            completion_tokens: 8,
            cached_tokens: null,
            total_tokens: 20,
            cost: 0.25,
            cost_available: true,
          },
          {
            id: 2,
            attempt: 2,
            provider: 'dashscope',
            model_name: 'qwen-plus',
            temperature: null,
            top_p: null,
            max_tokens: null,
            prompt_tokens: 5,
            completion_tokens: null,
            cached_tokens: null,
            total_tokens: null,
            cost: null,
            cost_available: false,
            error: { code: 'rate_limited' },
          },
        ]
      : [],
    tool_calls: isFailedTool
      ? [
          {
            id: 1,
            attempt: 2,
            tool_id: 'camera.grab',
            tool_name: '抓帧',
            input_arguments: { camera: 'cam-1' },
            output_result: null,
            execution_status: 'failed',
            permission_result: 'allowed',
            input_schema: { type: 'object' },
            output_schema: { type: 'object' },
            retry_count: 1,
            duration_ms: 200,
            error: { code: 'timeout' },
          },
        ]
      : [],
    retrieval_records: [],
    prompt_executions: isLlm
      ? [
          {
            id: 1,
            template_id: 7,
            template_version: 2,
            render_status: 'ok',
            model_name: 'qwen-plus',
            prompt_hash: 'a'.repeat(64),
            variables: { question: '设备状态' },
            rendered_messages: [{ role: 'user', content: '旧版消息' }],
          },
          {
            id: 2,
            template_id: 7,
            template_version: 3,
            render_status: 'ok',
            model_name: 'qwen-plus',
            prompt_hash: 'b'.repeat(64),
            variables: { question: '设备状态', locale: 'zh' },
            rendered_messages: [
              { role: 'system', content: 'S' },
              { role: 'user', content: '请分析设备' },
            ],
          },
        ]
      : [],
    meta: isLlm ? { model: 'qwen' } : null,
    links: null,
  }
}

function createTestRouter() {
  const Stub = defineComponent({ render: () => h('div') })
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      {
        path: '/observability/traces',
        name: 'observability-traces',
        component: Stub,
      },
      {
        path: '/observability/traces/:traceId',
        name: 'observability-trace-detail',
        component: TraceDetail,
      },
    ],
  })
}

async function mountPage(path = `/observability/traces/${TRACE_ID}`) {
  const pinia = createPinia()
  setActivePinia(pinia)
  const router = createTestRouter()
  await router.push(path)
  await router.isReady()

  const wrapper = mount(TraceDetail, {
    attachTo: document.body,
    global: {
      plugins: [ElementPlus, pinia, router],
      stubs: { teleport: true },
    },
  })
  await flushPromises()
  return { wrapper, router }
}

async function openTab(wrapper: ReturnType<typeof mount>, label: string) {
  const tab = wrapper
    .findAll('.el-tabs__item')
    .find((item) => item.text().trim() === label)
  expect(tab, `tab ${label} should exist`).toBeTruthy()
  await tab!.trigger('click')
  await flushPromises()
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(getTrace).mockResolvedValue(makeTraceDetail())
  vi.mocked(getTraceTree).mockResolvedValue(SPAN_TREE)
  vi.mocked(listTraceSpans).mockResolvedValue(FLAT_SPANS)
  vi.mocked(listTraceEvents).mockResolvedValue(EVENTS)
  vi.mocked(getSpan).mockImplementation((spanId: string) =>
    Promise.resolve(makeSpanDetail(spanId)),
  )
})

describe('Trace 详情三栏页', () => {
  it('点击第 3 个 Span 节点后检查器切换到该 Span，时间线条同步高亮', async () => {
    const { wrapper } = await mountPage()
    expect(wrapper.find('[data-test="trace-detail-left"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="trace-detail-center"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="trace-detail-right"]').exists()).toBe(true)

    const nodes = wrapper.findAll('[data-test^="span-tree-node-"]')
    expect(nodes).toHaveLength(5)
    expect(nodes[2].attributes('data-test')).toBe('span-tree-node-span-3')

    await nodes[2].trigger('click')
    await flushPromises()

    expect(getSpan).toHaveBeenCalledWith('span-3')
    expect(wrapper.find('[data-test="span-inspector-title"]').text()).toContain('模型调用')
    expect(wrapper.find('[data-test="span-inspector-id"]').text()).toBe('span-3')

    const bar = wrapper.find('[data-test="timeline-bar-span-3"]')
    expect(bar.exists()).toBe(true)
    expect(bar.classes()).toContain('span-timeline__bar--selected')

    // 点击时间线条同样触发选中与详情加载。
    await wrapper.find('[data-test="timeline-bar-span-4"]').trigger('click')
    await flushPromises()
    expect(getSpan).toHaveBeenCalledWith('span-4')
    expect(wrapper.find('[data-test="span-inspector-title"]').text()).toContain('工具调用失败')
    expect(
      wrapper.find('[data-test="timeline-bar-span-4"]').classes(),
    ).toContain('span-timeline__bar--selected')
  })

  it('失败节点及其祖先默认展开，失败 span 在树中直接可见', async () => {
    const { wrapper } = await mountPage()

    expect(wrapper.find('[data-test="span-tree-node-span-4"]').exists()).toBe(true)
    // span-5 是失败节点的子节点：失败节点被默认展开后也应可见。
    expect(wrapper.find('[data-test="span-tree-node-span-5"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="span-tree-node-span-1"]').exists()).toBe(true)
  })

  it('10 个 Tab 齐全，模型 Tab 展示真实 usage/cost，cost=null 显示费用不可用', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-test="span-tree-node-span-3"]').trigger('click')
    await flushPromises()

    const tabs = wrapper.findAll('.el-tabs__item')
    expect(tabs).toHaveLength(10)
    expect(tabs.map((tab) => tab.text().trim())).toEqual([
      '概览',
      '输入',
      '输出',
      '模型',
      '工具',
      '检索',
      'Prompt',
      '事件',
      '错误',
      '元数据',
    ])

    await openTab(wrapper, '模型')

    const cards = wrapper.findAll('[data-test="llm-call"]')
    expect(cards).toHaveLength(2)
    expect(cards[0].text()).toContain('qwen-plus')
    expect(cards[0].text()).toContain('temperature：0.2')
    expect(cards[0].text()).toContain('prompt_tokens：12')
    expect(cards[0].text()).toContain('completion_tokens：8')
    expect(cards[0].text()).toContain('total_tokens：20')

    const costs = wrapper.findAll('[data-test="llm-cost"]').map((node) => node.text())
    expect(costs[0]).toBe('cost：0.25')
    expect(costs[1]).toBe('cost：费用不可用')
  })

  it('事件 Tab 按序号展示事件并高亮当前 Span 的事件', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-test="span-tree-node-span-3"]').trigger('click')
    await flushPromises()
    await openTab(wrapper, '事件')

    const rows = wrapper.findAll('[data-test="event-row"]')
    expect(rows).toHaveLength(3)
    expect(rows[0].text()).toContain('1')
    expect(rows[0].text()).toContain('start')
    expect(rows[1].text()).toContain('2')
    expect(rows[1].text()).toContain('llm_call')

    const highlighted = rows.filter((row) =>
      row.classes().includes('span-inspector__event--current'),
    )
    expect(highlighted).toHaveLength(1)
    expect(highlighted[0].text()).toContain('llm_call')
  })

  it('Prompt Tab 展示 template_version、rendered_messages 与 variables 差异', async () => {
    const { wrapper } = await mountPage()
    await wrapper.find('[data-test="span-tree-node-span-3"]').trigger('click')
    await flushPromises()
    await openTab(wrapper, 'Prompt')

    const versions = wrapper
      .findAll('[data-test="prompt-template-version"]')
      .map((node) => node.text())
    expect(versions).toEqual(['v2', 'v3'])
    expect(wrapper.text()).toContain('请分析设备')
    expect(wrapper.text()).toContain('旧版消息')

    const diff = wrapper.find('[data-test="prompt-diff"]')
    expect(diff.exists()).toBe(true)
    expect(diff.text()).toContain('新增 key')
    expect(diff.text()).toContain('locale')
  })

  it('快速切换 Span 时旧 getSpan 响应不会覆盖新选择', async () => {
    const pending = new Map<string, (value: SpanDetail) => void>()
    vi.mocked(getSpan).mockImplementation(
      (spanId: string) =>
        new Promise<SpanDetail>((resolve) => {
          pending.set(spanId, resolve)
        }),
    )

    const { wrapper } = await mountPage()

    await wrapper.find('[data-test="span-tree-node-span-2"]').trigger('click')
    await nextTick()
    await wrapper.find('[data-test="span-tree-node-span-3"]').trigger('click')
    await nextTick()

    // 先返回较新的 span-3，再返回较旧的 span-2。
    pending.get('span-3')?.(makeSpanDetail('span-3'))
    await flushPromises()
    expect(wrapper.find('[data-test="span-inspector-title"]').text()).toContain('模型调用')

    pending.get('span-2')?.(makeSpanDetail('span-2'))
    await flushPromises()

    const title = wrapper.find('[data-test="span-inspector-title"]').text()
    expect(title).toContain('模型调用')
    expect(title).not.toContain('Agent 执行')
    expect(wrapper.find('[data-test="span-inspector-id"]').text()).toBe('span-3')
  })

  it('Span 树为空时显示空态，不渲染占位节点', async () => {
    vi.mocked(getTraceTree).mockResolvedValue([])
    vi.mocked(listTraceSpans).mockResolvedValue([])

    const { wrapper } = await mountPage()

    expect(wrapper.find('[data-test="async-empty"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('该 Trace 暂无 Span 数据')
    expect(wrapper.find('[data-test="span-tree"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="span-timeline"]').exists()).toBe(false)
  })

  it('API 失败时显示错误并可重试加载真实数据', async () => {
    vi.mocked(listTraceEvents).mockRejectedValueOnce(
      new ApiError(500, 500, '观测服务暂不可用'),
    )

    const { wrapper } = await mountPage()

    const errorState = wrapper.find('[data-test="async-error"]')
    expect(errorState.exists()).toBe(true)
    expect(errorState.text()).toContain('观测服务暂不可用')

    await errorState.find('button').trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="async-error"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="span-tree"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="span-timeline"]').exists()).toBe(true)
  })
})

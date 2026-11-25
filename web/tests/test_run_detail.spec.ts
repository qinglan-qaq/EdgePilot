import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import ElementPlus, { ElMessageBox } from 'element-plus'

import type { UserRole } from '../src/api/http'
import type { EventDetail } from '../src/api/events'
import type { SpanDetail, SpanNode, TraceDetail } from '../src/api/observability'
import type { WorkflowNodeRun, WorkflowRunDetail } from '../src/api/runs'

const authState = vi.hoisted(() => ({ role: 'operator' as UserRole }))
const routeState = vi.hoisted(() => ({ params: { id: '88' } as Record<string, unknown> }))
const routerPush = vi.hoisted(() => vi.fn())

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => authState,
}))

vi.mock('vue-router', () => ({
  useRoute: () => routeState,
  useRouter: () => ({ push: routerPush }),
}))

vi.mock('@/api/runs', () => ({
  getRun: vi.fn(),
  approveRun: vi.fn(),
  rejectRun: vi.fn(),
  cancelRun: vi.fn(),
}))

vi.mock('@/api/observability', () => ({
  getTrace: vi.fn(),
  getTraceTree: vi.fn(),
  getSpan: vi.fn(),
}))

vi.mock('@/api/events', () => ({
  getEvent: vi.fn(),
}))

import { getEvent } from '../src/api/events'
import { getSpan, getTrace, getTraceTree } from '../src/api/observability'
import { approveRun, cancelRun, getRun, rejectRun } from '../src/api/runs'
import RunDetail from '../src/views/RunDetail.vue'

enableAutoUnmount(afterEach)

function makeNode(overrides: Partial<WorkflowNodeRun> = {}): WorkflowNodeRun {
  return {
    id: 1,
    run_id: 88,
    node_id: 'approval_1',
    node_type: 'approval',
    status: 'waiting_approval',
    attempt: 1,
    input: { camera: 'cam-1' },
    output: null,
    approval: {
      title: '确认告警处置',
      approvers: ['operator'],
      created_at: '2026-11-22T10:00:00',
      expires_at: '2026-11-22T11:00:00',
      decision: null,
      by: null,
      at: null,
      comment: null,
    },
    error: null,
    span_id: null,
    heartbeat_at: null,
    started_at: '2026-11-22T10:00:00',
    finished_at: null,
    ...overrides,
  }
}

function makeRun(overrides: Partial<WorkflowRunDetail> = {}): WorkflowRunDetail {
  return {
    id: 88,
    workflow_id: 3,
    workflow_name: '河道巡检',
    workflow_version_id: 8,
    workflow_version: 2,
    status: 'waiting_approval',
    trigger_type: 'manual',
    trigger_payload: null,
    input: {},
    output: null,
    error: null,
    trace_id: null,
    root_span_id: null,
    created_at: '2026-11-22T09:59:00',
    started_at: '2026-11-22T10:00:00',
    finished_at: null,
    nodes: [makeNode()],
    ...overrides,
  }
}

function makeTrace(overrides: Partial<TraceDetail> = {}): TraceDetail {
  return {
    id: 1,
    trace_id: 'trace-1',
    name: 'workflow.河道巡检',
    source: 'workflow',
    user_id: 1,
    session_id: null,
    root_span_id: 'span-1',
    status: 'running',
    input: null,
    output: null,
    start_time: '2026-11-22T10:00:00',
    end_time: null,
    duration_ms: 1234,
    total_tokens: null,
    total_cost: null,
    cost_unavailable_count: 2,
    total_spans: 1,
    error_count: 0,
    meta: null,
    created_at: '2026-11-22T10:00:00',
    ...overrides,
  }
}

function makeSpanTree(): SpanNode[] {
  return [
    {
      span_id: 'span-1',
      trace_id: 'trace-1',
      parent_span_id: null,
      name: 'workflow.run',
      span_type: 'workflow',
      status: 'succeeded',
      duration_ms: 1234,
      node_id: 'agent_1',
      orphan: false,
      children: [],
    },
  ]
}

function makeSpanDetail(overrides: Partial<SpanDetail> = {}): SpanDetail {
  return {
    span_id: 'span-1',
    trace_id: 'trace-1',
    name: 'workflow.run',
    span_type: 'workflow',
    status: 'succeeded',
    duration_ms: 1234,
    input: { image: 'frame.jpg' },
    output: { risk: true },
    error: null,
    llm_calls: [
      {
        id: 1,
        provider: 'dashscope',
        model_name: 'qwen-vl-plus',
        total_tokens: 120,
        cost: null,
        cost_available: false,
      },
    ],
    tool_calls: [
      {
        id: 1,
        tool_id: 'camera.grab',
        tool_name: 'grab_frame',
        execution_status: 'succeeded',
      },
    ],
    retrievals: [],
    ...overrides,
  }
}

function makeEvent(overrides: Partial<EventDetail> = {}): EventDetail {
  return {
    id: 501,
    node_id: 7,
    device_id: 2,
    event_type: 'person_detected',
    severity: 'high',
    occurred_at: '2026-11-22T10:01:00',
    received_at: '2026-11-22T10:01:02',
    confidence: 0.93,
    track_id: 'trk-1',
    snapshot_path: '/snapshots/501.jpg',
    has_snapshot: true,
    bbox: { x: 10, y: 20, w: 30, h: 40 },
    model_version: 'yolo-v8',
    payload: { label: 'person' },
    dedup_key: 'cam-1:501',
    status: 'open',
    created_at: '2026-11-22T10:01:03',
    ...overrides,
  }
}

function mountPage() {
  return mount(RunDetail, {
    attachTo: document.body,
    global: {
      plugins: [ElementPlus],
      stubs: { teleport: true },
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  authState.role = 'operator'
  routeState.params = { id: '88' }
  vi.mocked(getRun).mockResolvedValue(makeRun())
  vi.mocked(approveRun).mockResolvedValue(
    makeRun({
      status: 'running',
      nodes: [
        makeNode({
          status: 'succeeded',
          approval: {
            title: '确认告警处置',
            approvers: ['operator'],
            decision: 'approved',
            by: 9,
            at: '2026-11-22T10:05:00',
            comment: '同意',
          },
          finished_at: '2026-11-22T10:05:00',
        }),
      ],
    }),
  )
  vi.mocked(rejectRun).mockResolvedValue(
    makeRun({
      status: 'failed',
      error: 'approval_rejected',
      finished_at: '2026-11-22T10:06:00',
      nodes: [
        makeNode({
          status: 'failed',
          error: 'approval_rejected',
          finished_at: '2026-11-22T10:06:00',
        }),
      ],
    }),
  )
})

describe('运行详情页', () => {
  it('审批人点击批准后调用真实接口，页面从待审批切换为运行中且按钮消失', async () => {
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('[data-test="run-status"]').text()).toContain('待审批')
    expect(wrapper.find('[data-test="approval-card"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('确认告警处置')
    expect(wrapper.text()).toContain('operator')

    const approveButton = wrapper.find('[data-test="approve-run"]')
    expect(approveButton.exists()).toBe(true)
    expect(wrapper.find('[data-test="reject-run"]').exists()).toBe(true)

    await wrapper.find('[data-test="approval-comment"]').setValue('同意')
    await approveButton.trigger('click')
    await flushPromises()

    expect(approveRun).toHaveBeenCalledTimes(1)
    expect(approveRun).toHaveBeenCalledWith(88, '同意')
    expect(wrapper.find('[data-test="run-status"]').text()).toContain('运行中')
    expect(wrapper.find('[data-test="approval-card"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="approve-run"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="reject-run"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('自动刷新')
  })

  it('admin 不在 approvers 中时不显示审批按钮，管理员不是隐式审批人', async () => {
    authState.role = 'admin'
    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('[data-test="approval-card"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="approve-run"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="reject-run"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="approval-forbidden"]').text()).toContain(
      '当前角色不在审批人列表',
    )
    expect(approveRun).not.toHaveBeenCalled()
  })

  it('审批人拒绝后展示真实失败状态与 approval_rejected', async () => {
    const wrapper = mountPage()
    await flushPromises()

    await wrapper.find('[data-test="approval-comment"]').setValue('现场风险过高')
    await wrapper.find('[data-test="reject-run"]').trigger('click')
    await flushPromises()

    expect(rejectRun).toHaveBeenCalledWith(88, '现场风险过高')
    expect(wrapper.find('[data-test="run-status"]').text()).toContain('失败')
    expect(wrapper.find('[data-test="approval-card"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="approve-run"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('approval_rejected')
  })

  it('operator 取消运行前二次确认，调用真实接口并切换为已取消', async () => {
    const confirm = vi
      .spyOn(ElMessageBox, 'confirm')
      .mockResolvedValue('confirm' as never)
    vi.mocked(getRun).mockResolvedValue(
      makeRun({
        status: 'running',
        nodes: [makeNode({ status: 'running', approval: null })],
      }),
    )
    vi.mocked(cancelRun).mockResolvedValue(
      makeRun({
        status: 'cancelled',
        finished_at: '2026-11-22T10:07:00',
        nodes: [makeNode({ status: 'skipped', approval: null })],
      }),
    )

    const wrapper = mountPage()
    await flushPromises()

    const cancelButton = wrapper.find('[data-test="cancel-run"]')
    expect(cancelButton.exists()).toBe(true)
    await cancelButton.trigger('click')
    await flushPromises()

    expect(confirm).toHaveBeenCalledTimes(1)
    expect(cancelRun).toHaveBeenCalledWith(88)
    expect(wrapper.find('[data-test="run-status"]').text()).toContain('已取消')
    expect(wrapper.find('[data-test="cancel-run"]').exists()).toBe(false)

    confirm.mockRestore()
  })

  it('Trace 抽屉展示真实汇总，null 费用/token 显示未上报，span 调用 getSpan 并标记费用不可用', async () => {
    vi.mocked(getRun).mockResolvedValue(
      makeRun({ trace_id: 'trace-1', root_span_id: 'span-1' }),
    )
    vi.mocked(getTrace).mockResolvedValue(makeTrace())
    vi.mocked(getTraceTree).mockResolvedValue(makeSpanTree())
    vi.mocked(getSpan).mockResolvedValue(makeSpanDetail())

    const wrapper = mountPage()
    await flushPromises()

    const openTrace = wrapper.find('[data-test="open-trace"]')
    expect(openTrace.exists()).toBe(true)
    await openTrace.trigger('click')
    await flushPromises()

    expect(getTrace).toHaveBeenCalledWith('trace-1')
    expect(getTraceTree).toHaveBeenCalledWith('trace-1')

    const summary = wrapper.find('[data-test="trace-summary"]')
    expect(summary.exists()).toBe(true)
    expect(summary.text()).toContain('workflow.河道巡检')
    expect(summary.text()).toContain('workflow')
    expect(summary.text()).toContain('1.2 秒')
    expect(summary.text()).toContain('未上报')
    expect(summary.text()).toContain('不估算')
    expect(wrapper.find('[data-test="trace-cost-unavailable"]').text()).toContain('2')

    const spanRow = wrapper.find('[data-test="span-node-span-1"]')
    expect(spanRow.exists()).toBe(true)
    await spanRow.trigger('click')
    await flushPromises()

    expect(getSpan).toHaveBeenCalledWith('span-1')
    const detail = wrapper.find('[data-test="span-detail"]')
    expect(detail.exists()).toBe(true)
    expect(detail.text()).toContain('qwen-vl-plus')
    expect(detail.text()).toContain('120')
    expect(detail.text()).toContain('费用未上报')
    expect(detail.text()).toContain('grab_frame')
    expect(detail.text()).toContain('camera.grab')
  })

  it('报告 Markdown 渲染，event_id 证据调用 getEvent，chunk_id 只展示片段且提示无详情接口', async () => {
    vi.mocked(getRun).mockResolvedValue(
      makeRun({
        status: 'succeeded',
        finished_at: '2026-11-22T10:10:00',
        output: {
          title: '巡检报告',
          markdown: '## 结论\n\n- 发现 **人员闯入**',
          finding_count: 3,
          highlights: [
            {
              statement: '人员闯入',
              evidence: [{ type: 'event_id', ref: '501', snippet: '检测到人员' }],
            },
          ],
          evidence: [
            { type: 'event_id', ref: '501', snippet: '检测到人员' },
            { type: 'chunk_id', ref: 'chunk-9', snippet: '应急预案第 3 条' },
          ],
        },
        nodes: [makeNode({ status: 'succeeded', approval: null })],
      }),
    )
    vi.mocked(getEvent).mockResolvedValue(makeEvent())

    const wrapper = mountPage()
    await flushPromises()

    const markdown = wrapper.find('[data-test="markdown-body"]')
    expect(markdown.exists()).toBe(true)
    expect(markdown.find('h2').text()).toBe('结论')
    expect(markdown.text()).toContain('人员闯入')
    expect(wrapper.text()).toContain('发现 3 项')
    const highlights = wrapper.find('[data-test="report-highlights"]')
    expect(highlights.exists()).toBe(true)
    expect(highlights.text()).toContain('人员闯入')
    expect(highlights.text()).toContain('event_id 501')

    const openEvidence = wrapper.find('[data-test="open-evidence"]')
    expect(openEvidence.exists()).toBe(true)
    expect(openEvidence.text()).toContain('查看证据 (2)')
    await openEvidence.trigger('click')
    await flushPromises()

    expect(wrapper.find('[data-test="evidence-item-0"]').text()).toContain('event_id')
    expect(wrapper.find('[data-test="evidence-item-0"]').text()).toContain('501')
    expect(wrapper.find('[data-test="evidence-item-1"]').text()).toContain('chunk-9')

    // 打开抽屉不自动请求，点击具体证据才请求
    expect(getEvent).not.toHaveBeenCalled()

    await wrapper.find('[data-test="evidence-view-0"]').trigger('click')
    await flushPromises()

    expect(getEvent).toHaveBeenCalledTimes(1)
    expect(getEvent).toHaveBeenCalledWith(501)
    const eventDetail = wrapper.find('[data-test="evidence-event-detail"]')
    expect(eventDetail.exists()).toBe(true)
    expect(eventDetail.text()).toContain('person_detected')
    expect(eventDetail.text()).toContain('high')
    expect(eventDetail.text()).toContain('2026-11-22 10:01')
    expect(eventDetail.text()).toContain('yolo-v8')
    expect(eventDetail.text()).toContain('快照：有')

    // chunk_id 的引用本身可点击查看，仍不请求事件接口
    await wrapper.find('[data-test="evidence-ref-1"]').trigger('click')
    await flushPromises()

    expect(getEvent).toHaveBeenCalledTimes(1)
    const chunk = wrapper.find('[data-test="evidence-chunk-note"]')
    expect(chunk.exists()).toBe(true)
    expect(chunk.text()).toContain('分块详情接口尚未提供')
    expect(wrapper.text()).toContain('应急预案第 3 条')
  })

  it('pending 运行只提示等待 worker 领取，不伪造 trace/耗时，viewer 无写操作', async () => {
    authState.role = 'viewer'
    vi.mocked(getRun).mockResolvedValue(
      makeRun({
        status: 'pending',
        started_at: null,
        finished_at: null,
        trace_id: null,
        nodes: [
          makeNode({
            status: 'pending',
            approval: null,
            started_at: null,
            input: null,
          }),
        ],
      }),
    )

    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('[data-test="run-pending-hint"]').text()).toContain(
      '等待 worker 领取',
    )
    expect(wrapper.find('[data-test="run-pending-hint"]').text()).toContain(
      '尚未采集到执行数据',
    )
    expect(wrapper.text()).toContain('自动刷新')
    expect(wrapper.find('[data-test="open-trace"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="trace-missing"]').text()).toContain('未采集')
    expect(wrapper.find('[data-test="run-duration"]').text()).toContain('—')
    expect(wrapper.find('[data-test="cancel-run"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="approve-run"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="reject-run"]').exists()).toBe(false)
    expect(getTrace).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('进行中')
  })
})

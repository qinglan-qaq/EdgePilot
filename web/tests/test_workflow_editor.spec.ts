import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { nextTick } from 'vue'

import type { UserRole } from '../src/api/http'
import type {
  ValidationReport,
  WorkflowDetail,
  WorkflowDsl,
  WorkflowRunDetail,
  WorkflowVersion,
} from '../src/api/workflows'

const authState = vi.hoisted(() => ({ role: 'operator' as UserRole }))
const routeState = vi.hoisted(() => ({ params: { id: '42' } as Record<string, unknown> }))
const routerPush = vi.hoisted(() => vi.fn())

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => authState,
}))

vi.mock('vue-router', () => ({
  useRoute: () => routeState,
  useRouter: () => ({ push: routerPush }),
  onBeforeRouteLeave: vi.fn(),
}))

vi.mock('@/api/workflows', () => ({
  createWorkflowRun: vi.fn(),
  getWorkflow: vi.fn(),
  listAgentsForPicker: vi.fn(),
  listToolsForPicker: vi.fn(),
  publishWorkflow: vi.fn(),
  updateWorkflow: vi.fn(),
  validateWorkflow: vi.fn(),
}))

import { ApiError } from '../src/api/http'
import {
  createWorkflowRun,
  getWorkflow,
  listAgentsForPicker,
  listToolsForPicker,
  publishWorkflow,
  updateWorkflow,
  validateWorkflow,
} from '../src/api/workflows'
import WorkflowEditor from '../src/views/WorkflowEditor.vue'

enableAutoUnmount(afterEach)

const EMPTY_DSL: WorkflowDsl = { nodes: [], edges: [] }

function makeDetail(overrides: Partial<WorkflowDetail> = {}): WorkflowDetail {
  return {
    id: 42,
    name: '巡检工作流',
    description: null,
    status: 'draft',
    current_version: 0,
    draft_version: 0,
    dsl: EMPTY_DSL,
    created_by: 1,
    created_at: '2026-11-22T09:00:00',
    updated_at: '2026-11-22T09:00:00',
    last_run: null,
    ...overrides,
  }
}

function makeReport(overrides: Partial<ValidationReport> = {}): ValidationReport {
  return {
    valid: true,
    errors: [],
    warnings: [],
    node_count: 0,
    edge_count: 0,
    entry_node: null,
    end_nodes: [],
    topological_order: [],
    ...overrides,
  }
}

function makeVersion(overrides: Partial<WorkflowVersion> = {}): WorkflowVersion {
  return {
    id: 8,
    workflow_id: 42,
    version: 1,
    dsl: EMPTY_DSL,
    dsl_hash: 'hash',
    created_by: 1,
    created_at: '2026-11-22T10:00:00',
    ...overrides,
  }
}

function makeRun(overrides: Partial<WorkflowRunDetail> = {}): WorkflowRunDetail {
  return {
    id: 77,
    workflow_id: 42,
    workflow_name: '巡检工作流',
    workflow_version_id: 8,
    workflow_version: 2,
    status: 'pending',
    trigger_type: 'manual',
    trigger_payload: null,
    input: {},
    output: null,
    error: null,
    trace_id: null,
    created_at: '2026-11-22T11:00:00',
    started_at: null,
    finished_at: null,
    ...overrides,
  }
}

function mountEditor(options: { stubTeleport?: boolean } = {}) {
  return mount(WorkflowEditor, {
    attachTo: document.body,
    global: {
      plugins: [ElementPlus],
      stubs: options.stubTeleport ? { teleport: true } : {},
    },
  })
}

interface EditorVm {
  applyConnection: (connection: {
    source: string
    target: string
    sourceHandle?: string | null
    targetHandle?: string | null
  }) => boolean
  updateNodeFields: (nodeId: string, patch: Record<string, unknown | undefined>) => void
  getCurrentDsl: () => WorkflowDsl
  getNodes: () => Array<{
    id: string
    data: { nodeType: string; fields: Record<string, unknown> }
  }>
  getEdges: () => Array<{ id: string; data: { when?: string } }>
}

interface FakeDataTransfer {
  dropEffect: string
  effectAllowed: string
  setData: (format: string, value: string) => void
  getData: (format: string) => string
}

/** 构造可记录数据的 DataTransfer 替身；派发的是真实 DOM 拖拽事件。 */
function createDataTransfer(): FakeDataTransfer {
  const store = new Map<string, string>()
  return {
    dropEffect: 'none',
    effectAllowed: 'all',
    setData(format, value) {
      store.set(format, value)
    },
    getData(format) {
      return store.get(format) ?? ''
    },
  }
}

function fireDragEvent(
  target: Element,
  type: string,
  dataTransfer: FakeDataTransfer,
  extra: Record<string, number> = {},
): void {
  const event = new Event(type, { bubbles: true, cancelable: true })
  Object.defineProperty(event, 'dataTransfer', { value: dataTransfer })
  for (const [key, value] of Object.entries(extra)) {
    Object.defineProperty(event, key, { value, configurable: true })
  }
  target.dispatchEvent(event)
}

/** 从节点面板真实 dragstart，再在画布上 dragover + drop。 */
async function dragNode(wrapper: VueWrapper, type: string): Promise<void> {
  const item = wrapper.find(`[data-test="palette-item-${type}"]`)
  expect(item.exists()).toBe(true)
  const dataTransfer = createDataTransfer()
  fireDragEvent(item.element, 'dragstart', dataTransfer)

  const canvas = wrapper.find('[data-test="workflow-canvas"]')
  expect(canvas.exists()).toBe(true)
  fireDragEvent(canvas.element, 'dragover', dataTransfer)
  // 非有限坐标用于走“投影失败回退网格坐标”路径，避免依赖 jsdom 布局。
  fireDragEvent(canvas.element, 'drop', dataTransfer, {
    clientX: Number.NaN,
    clientY: Number.NaN,
  })
  await nextTick()
  await flushPromises()
}

beforeEach(() => {
  vi.clearAllMocks()
  authState.role = 'operator'
  routeState.params = { id: '42' }

  vi.mocked(getWorkflow).mockResolvedValue(makeDetail())
  vi.mocked(listAgentsForPicker).mockResolvedValue([
    {
      id: 1,
      name: 'vision_agent',
      agent_type: 'vision',
      status: 'active',
      config: {},
      current_version: 1,
    },
    {
      id: 2,
      name: 'report_agent',
      agent_type: 'report',
      status: 'active',
      config: {},
      current_version: 1,
    },
  ])
  vi.mocked(listToolsForPicker).mockResolvedValue([])
  vi.mocked(updateWorkflow).mockImplementation(async (id, payload) =>
    makeDetail({
      id,
      dsl: payload.dsl ?? EMPTY_DSL,
      draft_version: 1,
    }),
  )
  vi.mocked(validateWorkflow).mockResolvedValue(makeReport())
  vi.mocked(publishWorkflow).mockResolvedValue(makeVersion())
  vi.mocked(createWorkflowRun).mockResolvedValue(makeRun())
})

describe('工作流编辑器交互', () => {

  it('真实拖拽六种节点、搭建 vision→condition→approval→report 图并校验', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    for (const type of ['start', 'agent', 'condition', 'approval', 'agent', 'end']) {
      await dragNode(wrapper, type)
    }

    const vm = wrapper.vm as unknown as EditorVm
    expect(vm.getNodes().map((node) => node.id)).toEqual([
      'start',
      'agent_1',
      'condition_1',
      'approval_1',
      'agent_2',
      'end',
    ])

    expect(wrapper.find('[data-test="workflow-node-start"]').exists()).toBe(true)
    const conditionCard = wrapper.find('[data-test="workflow-node-condition_1"]')
    expect(conditionCard.exists()).toBe(true)
    expect(conditionCard.text()).toContain('是')
    expect(conditionCard.text()).toContain('否')

    const conditionHandles = conditionCard.findAll('.vue-flow__handle')
    expect(
      conditionHandles.filter((handle) => handle.classes().includes('source')),
    ).toHaveLength(2)
    expect(
      wrapper
        .find('[data-test="workflow-node-start"]')
        .findAll('.vue-flow__handle')
        .filter((handle) => handle.classes().includes('target')),
    ).toHaveLength(0)
    expect(
      wrapper
        .find('[data-test="workflow-node-end"]')
        .findAll('.vue-flow__handle')
        .filter((handle) => handle.classes().includes('source')),
    ).toHaveLength(0)

    await dragNode(wrapper, 'start')
    expect(vm.getNodes()).toHaveLength(6)
    expect(wrapper.find('[data-test="editor-notice"]').text()).toContain(
      '只能有一个开始节点',
    )

    // 通过检查器共用的字段更新入口填入真实可校验的内容；
    // 结构校验仍由后端负责，这里只构造一份语义完整的图。
    vm.updateNodeFields('agent_1', { agent: 'vision_agent' })
    vm.updateNodeFields('condition_1', {
      expression: '$agent_1.output.risk == true',
    })
    vm.updateNodeFields('approval_1', {
      title: '确认告警处置',
      approvers: ['operator'],
    })
    vm.updateNodeFields('agent_2', { agent: 'report_agent' })
    await nextTick()

    expect(vm.applyConnection({ source: 'start', target: 'agent_1' })).toBe(true)
    expect(vm.applyConnection({ source: 'agent_1', target: 'condition_1' })).toBe(true)
    expect(
      vm.applyConnection({
        source: 'condition_1',
        target: 'approval_1',
        sourceHandle: 'true',
      }),
    ).toBe(true)
    expect(vm.applyConnection({ source: 'approval_1', target: 'agent_2' })).toBe(true)
    expect(vm.applyConnection({ source: 'agent_2', target: 'end' })).toBe(true)
    expect(
      vm.applyConnection({
        source: 'condition_1',
        target: 'end',
        sourceHandle: 'false',
      }),
    ).toBe(true)
    expect(vm.getEdges()).toHaveLength(6)

    vi.mocked(validateWorkflow).mockResolvedValue(
      makeReport({
        node_count: 6,
        edge_count: 6,
        entry_node: 'start',
        end_nodes: ['end'],
        topological_order: [
          'start',
          'agent_1',
          'condition_1',
          'approval_1',
          'agent_2',
          'end',
        ],
      }),
    )

    await wrapper.find('[data-test="validate-workflow"]').trigger('click')
    await flushPromises()

    expect(validateWorkflow).toHaveBeenCalledTimes(1)
    const [workflowId, dsl] = vi.mocked(validateWorkflow).mock.calls[0] ?? []
    expect(workflowId).toBe(42)
    expect(dsl).toBeDefined()
    expect(dsl?.nodes.map((node) => node.id)).toEqual([
      'start',
      'agent_1',
      'condition_1',
      'approval_1',
      'agent_2',
      'end',
    ])
    expect(dsl?.edges).toHaveLength(6)

    const nodeById = new Map((dsl?.nodes ?? []).map((node) => [node.id, node]))
    expect(nodeById.get('agent_1')?.agent).toBe('vision_agent')
    expect(nodeById.get('agent_1')?.max_retries).toBe(2)
    expect(nodeById.get('condition_1')?.expression).toBe(
      '$agent_1.output.risk == true',
    )
    expect(nodeById.get('approval_1')?.title).toBe('确认告警处置')
    expect(nodeById.get('approval_1')?.approvers).toEqual(['operator'])
    expect(nodeById.get('agent_2')?.agent).toBe('report_agent')

    const trueEdge = dsl?.edges.find(
      (edge) => edge.from === 'condition_1' && edge.when === 'true',
    )
    const falseEdge = dsl?.edges.find(
      (edge) => edge.from === 'condition_1' && edge.when === 'false',
    )
    expect(trueEdge?.to).toBe('approval_1')
    expect(falseEdge?.to).toBe('end')
    const plainEdge = dsl?.edges.find((edge) => edge.from === 'start')
    expect(Object.prototype.hasOwnProperty.call(plainEdge, 'when')).toBe(false)

    for (const node of dsl?.nodes ?? []) {
      const position = node.position as { x: number; y: number } | undefined
      expect(position).toBeDefined()
      expect(Number.isFinite(position?.x)).toBe(true)
      expect(Number.isFinite(position?.y)).toBe(true)
    }

    const summary = wrapper.find('[data-test="validation-summary"]')
    expect(summary.exists()).toBe(true)
    expect(wrapper.find('[data-test="validation-status"]').text()).toContain('校验通过')
    expect(summary.text()).toContain('节点 6')
    expect(summary.text()).toContain('连线 6')
    expect(summary.text()).toContain('start')
    expect(summary.text()).toContain('end')
  })

  it('查看者只读，无法拖入节点或保存、发布、试跑', async () => {
    authState.role = 'viewer'
    vi.mocked(getWorkflow).mockResolvedValue(makeDetail({ current_version: 2, status: 'published' }))
    const wrapper = mountEditor()
    await flushPromises()

    expect(wrapper.find('[data-test="validate-workflow"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="save-draft"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="publish-workflow"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="run-workflow"]').exists()).toBe(false)
    expect(wrapper.find('[data-test="workflow-canvas"]').attributes('data-readonly')).toBe(
      'true',
    )
    expect(
      wrapper.find('[data-test="palette-item-start"]').attributes('draggable'),
    ).toBe('false')

    await dragNode(wrapper, 'start')

    const vm = wrapper.vm as unknown as EditorVm
    expect(vm.getNodes()).toHaveLength(0)
    expect(wrapper.find('[data-test="editor-notice"]').text()).toContain('只读')
  })

  it('操作员可编辑但未发布时试跑按钮禁用并给出原因', async () => {
    const wrapper = mountEditor()
    await flushPromises()

    expect(wrapper.find('[data-test="save-draft"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="publish-workflow"]').exists()).toBe(false)

    const runButton = wrapper.find('[data-test="run-workflow"]')
    expect(runButton.exists()).toBe(true)
    expect(runButton.attributes('disabled')).toBeDefined()
    expect(wrapper.find('[data-test="run-hint"]').text()).toContain('尚未发布')

    await runButton.trigger('click')
    await flushPromises()
    expect(createWorkflowRun).not.toHaveBeenCalled()
    expect(wrapper.find('[data-test="run-input"]').exists()).toBe(false)
  })

  it('管理员发布前先保存草稿，成功后展示真实版本', async () => {
    authState.role = 'admin'
    const wrapper = mountEditor()
    await flushPromises()

    const publishButton = wrapper.find('[data-test="publish-workflow"]')
    expect(publishButton.exists()).toBe(true)
    await publishButton.trigger('click')
    await flushPromises()

    expect(updateWorkflow).toHaveBeenCalledTimes(1)
    expect(publishWorkflow).toHaveBeenCalledTimes(1)
    expect(updateWorkflow).toHaveBeenCalledWith(42, { dsl: EMPTY_DSL })
    expect(
      vi.mocked(updateWorkflow).mock.invocationCallOrder[0] ?? Number.MAX_SAFE_INTEGER,
    ).toBeLessThan(
      vi.mocked(publishWorkflow).mock.invocationCallOrder[0] ?? 0,
    )
    expect(wrapper.find('[data-test="editor-notice"]').text()).toContain(
      '发布成功，当前版本 v1',
    )
  })

  it('发布返回 400 时自动校验并展示真实报告，不展示成功', async () => {
    authState.role = 'admin'
    vi.mocked(publishWorkflow).mockRejectedValue(
      new ApiError(400, 400, '草稿校验未通过，无法发布'),
    )
    vi.mocked(validateWorkflow).mockResolvedValue(
      makeReport({
        valid: false,
        errors: [
          {
            code: 'missing_start',
            message: '工作流缺少 start 节点',
            node_id: 'agent_1',
          },
        ],
        node_count: 1,
        edge_count: 0,
      }),
    )

    const wrapper = mountEditor()
    await flushPromises()

    await wrapper.find('[data-test="publish-workflow"]').trigger('click')
    await flushPromises()

    expect(updateWorkflow).toHaveBeenCalledTimes(1)
    expect(publishWorkflow).toHaveBeenCalledTimes(1)
    expect(validateWorkflow).toHaveBeenCalledTimes(1)
    expect(wrapper.find('[data-test="editor-notice"]').text()).toContain(
      '草稿校验未通过，无法发布',
    )
    expect(wrapper.find('[data-test="editor-notice"]').text()).not.toContain('发布成功')
    expect(wrapper.find('[data-test="validation-status"]').text()).toContain('校验未通过')
    expect(wrapper.find('[data-test="validation-errors"]').text()).toContain(
      '工作流缺少 start 节点',
    )
  })

  it('已发布工作流可提交 JSON 对象并只展示后端返回的运行信息', async () => {
    vi.mocked(getWorkflow).mockResolvedValue(
      makeDetail({ current_version: 2, status: 'published', draft_version: 2 }),
    )
    vi.mocked(createWorkflowRun).mockResolvedValue(makeRun())
    const wrapper = mountEditor()
    await flushPromises()

    const runButton = wrapper.find('[data-test="run-workflow"]')
    expect(runButton.attributes('disabled')).toBeUndefined()
    await runButton.trigger('click')
    await nextTick()
    await flushPromises()

    // ElInput 将外部 attrs 透传到内部 textarea 上。
    const input = wrapper.find('[data-test="run-input"]')
    expect(input.exists()).toBe(true)
    await input.setValue('{"camera":"cam-1","threshold":0.8}')

    await wrapper.find('[data-test="submit-run"]').trigger('click')
    await flushPromises()

    expect(createWorkflowRun).toHaveBeenCalledTimes(1)
    expect(createWorkflowRun).toHaveBeenCalledWith(42, {
      input: { camera: 'cam-1', threshold: 0.8 },
    })

    const result = wrapper.find('[data-test="run-result"]')
    expect(result.exists()).toBe(true)
    expect(result.text()).toContain('运行 ID：77')
    expect(result.text()).toContain('pending')
    expect(result.text()).toContain('v2')
    expect(wrapper.find('[data-test="run-error"]').exists()).toBe(false)
  })

  it('非法 JSON 不发起运行请求并显示解析错误', async () => {
    vi.mocked(getWorkflow).mockResolvedValue(
      makeDetail({ current_version: 2, status: 'published' }),
    )
    const wrapper = mountEditor()
    await flushPromises()

    await wrapper.find('[data-test="run-workflow"]').trigger('click')
    await nextTick()
    await flushPromises()

    await wrapper.find('[data-test="run-input"]').setValue('{oops')
    await wrapper.find('[data-test="submit-run"]').trigger('click')
    await flushPromises()

    expect(createWorkflowRun).not.toHaveBeenCalled()
    expect(wrapper.find('[data-test="run-input-error"]').text()).toContain('JSON 解析失败')
  })
})

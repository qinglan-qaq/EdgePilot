<script setup lang="ts">
import { computed, onMounted, ref, shallowRef, watch } from 'vue'
import { onBeforeRouteLeave, useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import {
  VueFlow,
  useVueFlow,
  type Connection,
  type GraphNode,
  type NodeMouseEvent,
  type XYPosition,
} from '@vue-flow/core'
import { Background } from '@vue-flow/background'
import { Controls } from '@vue-flow/controls'
import { MiniMap } from '@vue-flow/minimap'
import '@vue-flow/core/dist/style.css'
import '@vue-flow/core/dist/theme-default.css'
import '@vue-flow/controls/dist/style.css'
import '@vue-flow/minimap/dist/style.css'

import { ApiError } from '@/api/http'
import {
  createWorkflowRun,
  getWorkflow,
  listAgentsForPicker,
  listToolsForPicker,
  publishWorkflow,
  updateWorkflow,
  validateWorkflow,
  type AgentPickerItem,
  type ToolPickerItem,
  type ValidationReport,
  type WorkflowDetail,
  type WorkflowDsl,
  type WorkflowRunDetail,
  type WorkflowStatus,
} from '@/api/workflows'
import AsyncState from '@/components/common/AsyncState.vue'
import NodeInspector from '@/components/workflow/NodeInspector.vue'
import NodePalette from '@/components/workflow/NodePalette.vue'
import ValidationPanel from '@/components/workflow/ValidationPanel.vue'
import WorkflowNodeCard from '@/components/workflow/WorkflowNodeCard.vue'
import {
  connectFlowNodes,
  createFlowNode,
  dslToFlow,
  flowToDsl,
  isWorkflowNodeType,
  nodeTypeMeta,
  stableStringify,
  upstreamNodeIds,
  WORKFLOW_NODE_DRAG_TYPE,
  type FlowEdge,
  type FlowNode,
  type WorkflowNodeData,
  type WorkflowNodeType,
} from '@/components/workflow/editor-model'
import { useAuthStore } from '@/stores/auth'

type TagType = 'success' | 'info' | 'warning' | 'danger' | undefined
type NoticeType = 'success' | 'warning' | 'error' | 'info'

interface EditorNotice {
  type: NoticeType
  message: string
}

const DROP_GRID_COLUMNS = 3
const DROP_ORIGIN_X = 80
const DROP_ORIGIN_Y = 80
const DROP_GRID_X = 280
const DROP_GRID_Y = 150
const PALETTE_GRID_COLUMNS = 2
const PALETTE_GRID_X = 300
const PALETTE_GRID_Y = 160

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()
const { screenToFlowCoordinate } = useVueFlow()

/** 从路由参数读取正整数工作流 id；非法时返回 null。 */
function readWorkflowId(): number | null {
  const raw = route.params.id
  const value = Array.isArray(raw) ? raw[0] : raw
  if (typeof value !== 'string' || !/^\d+$/.test(value)) {
    return null
  }
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null
}

const workflowId = computed(() => readWorkflowId())
const invalidId = computed(() => workflowId.value === null)

const loading = ref(true)
const loadError = ref<string | null>(null)
const workflow = ref<WorkflowDetail | null>(null)
const currentVersion = ref(0)
const draftVersion = ref<number | null>(null)
const workflowStatus = ref<WorkflowStatus>('draft')

// Vue Flow 的 Node / Edge 类型含递归结构，用 shallowRef 避免深层类型实例化，
// 同时 v-model 每次都会回传新数组，仍能触发 dirty 计算。
const nodes = shallowRef<FlowNode[]>([])
const edges = shallowRef<FlowEdge[]>([])
const savedSnapshot = ref(stableStringify(flowToDsl([], [])))
const selectedNodeId = ref<string | null>(null)

const agents = ref<AgentPickerItem[]>([])
const tools = ref<ToolPickerItem[]>([])
const pickersWarning = ref<string | null>(null)

const notice = ref<EditorNotice | null>(null)
const validating = ref(false)
const validationReport = ref<ValidationReport | null>(null)
const validationError = ref<string | null>(null)
const saving = ref(false)
const publishing = ref(false)

const runDialogVisible = ref(false)
const runInputText = ref('{}')
const runInputError = ref('')
const runError = ref('')
const running = ref(false)
const runResult = ref<WorkflowRunDetail | null>(null)

let loadToken = 0
let fallbackDropIndex = 0

const canEdit = computed(() => auth.role === 'operator' || auth.role === 'admin')
const canPublish = computed(() => auth.role === 'admin')
const canRun = computed(() => canEdit.value)

const hasStart = computed(() => nodes.value.some((node) => node.data?.nodeType === 'start'))
const currentDsl = computed<WorkflowDsl>(() => flowToDsl(nodes.value, edges.value))
const dirty = computed(() => stableStringify(currentDsl.value) !== savedSnapshot.value)
const selectedNode = computed(
  () => nodes.value.find((node) => node.id === selectedNodeId.value) ?? null,
)
const selectedUpstreamIds = computed(() => {
  if (!selectedNode.value) {
    return []
  }
  return upstreamNodeIds(selectedNode.value.id, nodes.value, edges.value)
})

const statusLabel = computed(() => (workflowStatus.value === 'published' ? '已发布' : '草稿'))
const statusTagType = computed<TagType>(() =>
  workflowStatus.value === 'published' ? 'success' : 'info',
)
const isPublished = computed(() => currentVersion.value >= 1)
const canStartRun = computed(() => canRun.value && isPublished.value)

/** 试跑弹窗中的版本提示：明确始终使用已发布版本。 */
const runVersionHint = computed(() => {
  if (!isPublished.value) {
    return '工作流尚未发布，无法试跑。'
  }
  if (dirty.value) {
    return `当前画布有未保存修改；本次试跑使用已发布版本 v${currentVersion.value}。`
  }
  if (draftVersion.value !== null && draftVersion.value > currentVersion.value) {
    return `当前草稿版本 v${draftVersion.value} 尚未发布；本次试跑使用已发布版本 v${currentVersion.value}。`
  }
  return ''
})

/** 只展示后端 ApiError.message 或网络错误文案。 */
function errorToMessage(value: unknown): string {
  if (value instanceof ApiError) {
    return value.message
  }
  if (value instanceof Error && value.message) {
    return value.message
  }
  return '请求失败，请稍后重试'
}

function setNotice(type: NoticeType, message: string): void {
  notice.value = { type, message }
}

/** 应用详情响应：重建画布并记录 dirty 快照。 */
function applyDetail(detail: WorkflowDetail): void {
  workflow.value = detail
  currentVersion.value = detail.current_version
  draftVersion.value = detail.draft_version
  workflowStatus.value = detail.status

  const flow = dslToFlow(detail.dsl)
  nodes.value = flow.nodes
  edges.value = flow.edges
  selectedNodeId.value = null
  validationReport.value = null
  validationError.value = null
  notice.value = null
  fallbackDropIndex = 0
  savedSnapshot.value = stableStringify(flowToDsl(flow.nodes, flow.edges))
}

async function loadWorkflow(): Promise<void> {
  const id = workflowId.value
  if (id === null) {
    loading.value = false
    return
  }
  const token = ++loadToken
  loading.value = true
  loadError.value = null
  try {
    const detail = await getWorkflow(id)
    if (token !== loadToken) {
      return
    }
    applyDetail(detail)
  } catch (cause) {
    if (token !== loadToken) {
      return
    }
    workflow.value = null
    loadError.value = errorToMessage(cause)
  } finally {
    if (token === loadToken) {
      loading.value = false
    }
  }
}

/** Agent / Tool 选择器失败不阻塞编辑，只给出 warning。 */
async function loadPickers(): Promise<void> {
  const [agentResult, toolResult] = await Promise.allSettled([
    listAgentsForPicker(),
    listToolsForPicker(),
  ])
  const problems: string[] = []

  if (agentResult.status === 'fulfilled') {
    agents.value = agentResult.value
  } else {
    agents.value = []
    problems.push(`Agent 选择器不可用：${errorToMessage(agentResult.reason)}`)
  }

  if (toolResult.status === 'fulfilled') {
    tools.value = toolResult.value
  } else {
    tools.value = []
    problems.push(`工具选择器不可用：${errorToMessage(toolResult.reason)}`)
  }

  pickersWarning.value =
    problems.length > 0
      ? `${problems.join('；')}。你仍可编辑已有节点，但暂时无法从列表中选择。`
      : null
}

/** 拖拽经过画布时允许放置。 */
function handleDragOver(event: DragEvent): void {
  event.preventDefault()
  if (event.dataTransfer) {
    event.dataTransfer.dropEffect = canEdit.value ? 'move' : 'none'
  }
}

/** 视口坐标投影失败时使用的递增网格坐标。 */
function fallbackDropPosition(): XYPosition {
  const index = fallbackDropIndex
  fallbackDropIndex += 1
  return {
    x: DROP_ORIGIN_X + (index % DROP_GRID_COLUMNS) * DROP_GRID_X,
    y: DROP_ORIGIN_Y + Math.floor(index / DROP_GRID_COLUMNS) * DROP_GRID_Y,
  }
}

/** 把屏幕坐标转换为画布坐标；失败或非有限值时回退网格坐标。 */
function resolveDropPosition(event: DragEvent): XYPosition {
  const clientX = Number(event.clientX)
  const clientY = Number(event.clientY)
  if (Number.isFinite(clientX) && Number.isFinite(clientY)) {
    try {
      const projected = screenToFlowCoordinate({ x: clientX, y: clientY })
      if (
        projected &&
        Number.isFinite(projected.x) &&
        Number.isFinite(projected.y)
      ) {
        return { x: projected.x, y: projected.y }
      }
    } catch {
      // 视口尚未初始化时使用回退坐标
    }
  }
  return fallbackDropPosition()
}

/** 在指定画布坐标添加节点；start 重复时提示并拒绝。 */
function addNodeAt(type: WorkflowNodeType, position: XYPosition): void {
  if (!canEdit.value) {
    setNotice('warning', '当前角色为只读，无法添加节点。')
    return
  }
  if (type === 'start' && hasStart.value) {
    setNotice('warning', '已存在 start 节点，工作流只能有一个开始节点。')
    return
  }
  const node = createFlowNode(type, nodes.value, position)
  if (!node) {
    setNotice('warning', '已存在 start 节点，工作流只能有一个开始节点。')
    return
  }
  nodes.value = [...nodes.value, node]
  selectedNodeId.value = node.id
  notice.value = null
}

/** 面板点击添加：使用递增网格坐标。 */
function addFromPalette(type: WorkflowNodeType): void {
  const index = nodes.value.length
  addNodeAt(type, {
    x: DROP_ORIGIN_X + (index % PALETTE_GRID_COLUMNS) * PALETTE_GRID_X,
    y: DROP_ORIGIN_Y + Math.floor(index / PALETTE_GRID_COLUMNS) * PALETTE_GRID_Y,
  })
}

/** HTML5 drop 入口；必须能接收真实 dragstart 写入的节点类型。 */
function handleDrop(event: DragEvent): void {
  event.preventDefault()
  if (!canEdit.value) {
    setNotice('warning', '当前角色为只读，无法添加节点。')
    return
  }
  const rawType =
    event.dataTransfer?.getData(WORKFLOW_NODE_DRAG_TYPE) ||
    event.dataTransfer?.getData('text/plain') ||
    ''
  if (!isWorkflowNodeType(rawType)) {
    return
  }
  addNodeAt(rawType, resolveDropPosition(event))
}

/** 选中节点；点击画布空白处清除选中。 */
function handleNodeClick(event: NodeMouseEvent): void {
  selectedNodeId.value = event.node.id
}

function handlePaneClick(): void {
  selectedNodeId.value = null
}

/** 检查一条候选连线是否可创建（用于 Vue Flow 拖拽时即时反馈）。 */
function isValidConnection(connection: Connection): boolean {
  if (!canEdit.value) {
    return false
  }
  return connectFlowNodes(nodes.value, edges.value, connection).error === null
}

/**
 * 创建连线；模板 @connect 与 defineExpose 暴露的测试接缝共用此函数。
 *
 * 关键节点：
 * 1. 只读角色不能改图，直接拒绝；
 * 2. 连线合法性（起止节点、是否重复、是否自环）由 `connectFlowNodes` 统一判定；
 * 3. 非法连线只提示、不落地，避免画布出现后端校验不过的 DSL。
 *
 * @param connection Vue Flow 提供的起止节点与句柄信息。
 * @returns 是否成功加入画布。
 */
function applyConnection(connection: Connection): boolean {
  if (!canEdit.value) {
    return false
  }
  const result = connectFlowNodes(nodes.value, edges.value, connection)
  if (result.error) {
    setNotice('warning', result.error)
    return false
  }
  edges.value = result.edges
  return true
}

/** 检查器字段修改：合并已知字段，undefined 表示删除该键。 */
function handlePatch(nodeId: string, patch: Record<string, unknown | undefined>): void {
  if (!canEdit.value) {
    return
  }
  const index = nodes.value.findIndex((node) => node.id === nodeId)
  if (index < 0) {
    return
  }
  const node = nodes.value[index]
  const fields = { ...node.data.fields }
  for (const [key, value] of Object.entries(patch)) {
    if (value === undefined) {
      delete fields[key]
    } else {
      fields[key] = value
    }
  }
  const nextNodes = [...nodes.value]
  nextNodes[index] = { ...node, data: { ...node.data, fields } }
  nodes.value = nextNodes
}

/** 校验当前画布 DSL；所有登录角色均可调用。 */
async function runValidation(): Promise<void> {
  const id = workflowId.value
  if (id === null) {
    return
  }
  validating.value = true
  validationError.value = null
  try {
    const report = await validateWorkflow(id, currentDsl.value)
    validationReport.value = report
  } catch (cause) {
    validationReport.value = null
    validationError.value = errorToMessage(cause)
  } finally {
    validating.value = false
  }
}

/** 选中带 node_id 的校验问题。 */
function handleSelectIssue(nodeId: string): void {
  if (!nodes.value.some((node) => node.id === nodeId)) {
    setNotice('warning', `节点 ${nodeId} 当前不在画布中，无法定位。`)
    return
  }
  selectedNodeId.value = nodeId
}

/**
 * 保存草稿；发布流程复用本函数。
 *
 * 关键节点：
 * 1. 只读、无 id、正在保存时不重复提交；
 * 2. 保存成功后用后端返回的版本/状态刷新页面，并记录已保存快照；
 * 3. 已保存快照用于判断“是否有未保存改动”，离开页面前提示。
 *
 * @param announce 是否弹出成功提示；发布流程传 false 避免双提示。
 * @returns 是否保存成功。
 */
async function saveDraft(announce = true): Promise<boolean> {
  const id = workflowId.value
  if (id === null || !canEdit.value || saving.value) {
    return false
  }
  saving.value = true
  notice.value = null
  const dsl = currentDsl.value
  try {
    const updated = await updateWorkflow(id, { dsl })
    currentVersion.value = updated.current_version
    draftVersion.value = updated.draft_version
    workflowStatus.value = updated.status
    if (workflow.value) {
      workflow.value = {
        ...workflow.value,
        status: updated.status,
        current_version: updated.current_version,
        draft_version: updated.draft_version,
        updated_at: updated.updated_at,
      }
    }
    savedSnapshot.value = stableStringify(dsl)
    if (announce) {
      setNotice('success', '草稿已保存。')
    }
    return true
  } catch (cause) {
    setNotice('error', errorToMessage(cause))
    return false
  } finally {
    saving.value = false
  }
}

/**
 * 发布：先保存草稿，再调用发布接口。
 *
 * 关键节点：
 * 1. 必须先把画布保存成草稿，否则发布的是旧 DSL；
 * 2. 发布成功后刷新当前版本与状态，页面的“可试跑”条件随之满足；
 * 3. 后端返回 400 说明 DSL 校验未通过，这里自动拉取校验明细给用户定位。
 */
async function handlePublish(): Promise<void> {
  const id = workflowId.value
  if (id === null || !canPublish.value || publishing.value) {
    return
  }
  const saved = await saveDraft(false)
  if (!saved) {
    return
  }

  publishing.value = true
  notice.value = null
  try {
    const version = await publishWorkflow(id)
    currentVersion.value = version.version
    workflowStatus.value = 'published'
    if (workflow.value) {
      workflow.value = {
        ...workflow.value,
        status: 'published',
        current_version: version.version,
      }
    }
    setNotice('success', `发布成功，当前版本 v${version.version}。`)
  } catch (cause) {
    setNotice('error', errorToMessage(cause))
    if (cause instanceof ApiError && cause.status === 400) {
      await runValidation()
    }
  } finally {
    publishing.value = false
  }
}

function openRunDialog(): void {
  if (!canStartRun.value) {
    return
  }
  runInputText.value = '{}'
  runInputError.value = ''
  runError.value = ''
  runResult.value = null
  runDialogVisible.value = true
}

/** 解析试跑输入：空文本视为 {}，必须是 JSON 对象。 */
function parseRunInput(): Record<string, unknown> | null {
  const text = runInputText.value.trim()
  if (!text) {
    return {}
  }
  let parsed: unknown
  try {
    parsed = JSON.parse(text)
  } catch (cause) {
    const reason = cause instanceof Error ? cause.message : '未知解析错误'
    runInputError.value = `JSON 解析失败：${reason}`
    return null
  }
  if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
    runInputError.value = '运行输入必须是 JSON 对象，例如 {"camera": "cam-1"}。'
    return null
  }
  return parsed as Record<string, unknown>
}

/**
 * 创建 pending 运行记录；本页不轮询执行进度。
 *
 * 关键节点：
 * 1. 输入必须先通过 JSON 对象校验，空文本按 `{}` 处理；
 * 2. 后端只负责创建 run 并投递 worker，真正的执行发生在 Celery 侧；
 * 3. 页面把返回的运行详情展示给用户，进度需要到运行详情页查看。
 */
async function submitRun(): Promise<void> {
  const id = workflowId.value
  if (id === null || !canStartRun.value || running.value) {
    return
  }
  runInputError.value = ''
  runError.value = ''
  const input = parseRunInput()
  if (!input) {
    return
  }
  running.value = true
  try {
    const result = await createWorkflowRun(id, { input })
    runResult.value = result
  } catch (cause) {
    runError.value = errorToMessage(cause)
  } finally {
    running.value = false
  }
}

function goBack(): void {
  void router.push({ name: 'workflows' })
}

/** MiniMap 节点颜色与节点类型保持一致。 */
function minimapNodeColor(node: GraphNode): string {
  const data = node.data as WorkflowNodeData | undefined
  return data ? (nodeTypeMeta(data.nodeType)?.color ?? '#94a3b8') : '#94a3b8'
}

// 选中节点被删除后自动清空检查器
watch(nodes, (list) => {
  if (selectedNodeId.value && !list.some((node) => node.id === selectedNodeId.value)) {
    selectedNodeId.value = null
  }
})

watch(workflowId, () => {
  if (workflowId.value === null) {
    loading.value = false
    return
  }
  void loadWorkflow()
  void loadPickers()
})

onBeforeRouteLeave(async () => {
  if (!dirty.value) {
    return true
  }
  try {
    await ElMessageBox.confirm(
      '当前有未保存的修改，离开后这些修改将丢失。',
      '离开确认',
      {
        confirmButtonText: '离开',
        cancelButtonText: '继续编辑',
        type: 'warning',
      },
    )
    return true
  } catch {
    return false
  }
})

onMounted(() => {
  if (workflowId.value === null) {
    loading.value = false
    return
  }
  void loadWorkflow()
  void loadPickers()
})

defineExpose({
  /**
   * 图操作测试接缝：与模板 @connect 使用同一个连线处理函数，
   * 确保测试验证的是真实组件行为。
   */
  applyConnection,
  /** 与检查器 @patch 共用的字段更新入口，供测试构造真实可校验的图。 */
  updateNodeFields: handlePatch,
  /** 读取当前画布 DSL，便于测试断言真实结构。 */
  getCurrentDsl: () => currentDsl.value,
  getNodes: () => nodes.value,
  getEdges: () => edges.value,
})
</script>

<template>
  <section class="editor-page">
    <el-card v-if="invalidId" shadow="never" class="editor-page__card" data-test="invalid-workflow-id">
      <el-result icon="error" title="无效的工作流 ID" sub-title="路由参数必须是正整数。">
        <template #extra>
          <el-button type="primary" @click="goBack">返回工作流列表</el-button>
        </template>
      </el-result>
    </el-card>

    <el-card v-else shadow="never" class="editor-page__card">
      <AsyncState :loading="loading" :error="loadError" :empty="false" @retry="loadWorkflow()">
        <div v-if="workflow" class="editor">
          <header class="editor__toolbar">
            <div class="editor__heading">
              <el-button link type="primary" data-test="back-to-list" @click="goBack">
                返回列表
              </el-button>
              <h2 class="editor__title">{{ workflow.name }}</h2>
              <el-tag :type="statusTagType" effect="light">{{ statusLabel }}</el-tag>
              <el-tag v-if="dirty" type="warning" effect="plain" data-test="dirty-indicator">
                有未保存修改
              </el-tag>
              <span class="editor__meta">
                当前版本 {{ currentVersion > 0 ? `v${currentVersion}` : '未发布' }}
              </span>
              <span v-if="draftVersion !== null" class="editor__meta">
                草稿版本 {{ draftVersion }}
              </span>
            </div>

            <div class="editor__actions">
              <el-button
                data-test="validate-workflow"
                :loading="validating"
                @click="runValidation"
              >
                校验
              </el-button>
              <el-button
                v-if="canEdit"
                data-test="save-draft"
                type="primary"
                :loading="saving"
                @click="saveDraft()"
              >
                保存草稿
              </el-button>
              <el-button
                v-if="canPublish"
                data-test="publish-workflow"
                type="success"
                :loading="publishing"
                @click="handlePublish"
              >
                发布
              </el-button>
              <el-tooltip
                v-if="canRun"
                :disabled="isPublished"
                content="需先发布工作流后才能试跑"
                placement="top"
              >
                <span class="editor__run-wrap">
                  <el-button
                    data-test="run-workflow"
                    type="info"
                    :disabled="!canStartRun"
                    @click="openRunDialog"
                  >
                    试跑
                  </el-button>
                </span>
              </el-tooltip>
            </div>
          </header>

          <el-alert
            v-if="notice"
            :type="notice.type"
            :title="notice.message"
            show-icon
            class="editor__notice"
            data-test="editor-notice"
            @close="notice = null"
          />
          <el-alert
            v-if="pickersWarning"
            type="warning"
            :title="pickersWarning"
            :closable="false"
            show-icon
            class="editor__notice"
            data-test="picker-warning"
          />
          <p v-if="canRun && !isPublished" class="editor__run-hint" data-test="run-hint">
            工作流尚未发布，需先发布后才能试跑。
          </p>

          <div class="editor__body">
            <aside class="editor__palette">
              <NodePalette :disabled="!canEdit" :has-start="hasStart" @add="addFromPalette" />
            </aside>

            <div
              class="editor__canvas"
              data-test="workflow-canvas"
              :data-readonly="!canEdit"
              @dragover.prevent="handleDragOver"
              @drop.prevent="handleDrop"
            >
              <VueFlow
                v-model:nodes="nodes"
                v-model:edges="edges"
                :nodes-draggable="canEdit"
                :nodes-connectable="canEdit"
                :elements-selectable="true"
                :delete-key-code="canEdit ? 'Delete' : null"
                :is-valid-connection="isValidConnection"
                :min-zoom="0.3"
                :max-zoom="2"
                :default-viewport="{ zoom: 0.85 }"
                fit-view-on-init
                class="editor__flow"
                @node-click="handleNodeClick"
                @pane-click="handlePaneClick"
                @connect="applyConnection"
              >
                <Background :gap="18" :size="1" pattern-color="#dbe3ef" />
                <Controls />
                <MiniMap :pannable="true" :zoomable="true" :node-color="minimapNodeColor" />
                <template #node-workflow="nodeProps">
                  <WorkflowNodeCard v-bind="nodeProps" />
                </template>
              </VueFlow>

              <div v-if="nodes.length === 0" class="editor__canvas-empty">
                从左侧拖入六种节点开始编排。
              </div>
            </div>

            <aside class="editor__side">
              <NodeInspector
                :node="selectedNode"
                :agents="agents"
                :tools="tools"
                :disabled="!canEdit"
                :picker-warning="pickersWarning"
                :upstream-ids="selectedUpstreamIds"
                @patch="handlePatch"
              />
              <ValidationPanel
                :report="validationReport"
                :error="validationError"
                :validating="validating"
                @validate="runValidation"
                @select-node="handleSelectIssue"
              />
            </aside>
          </div>
        </div>
      </AsyncState>
    </el-card>

    <el-dialog v-model="runDialogVisible" title="试跑工作流" width="540px">
      <el-alert
        v-if="runVersionHint"
        type="warning"
        :title="runVersionHint"
        :closable="false"
        show-icon
        class="editor__dialog-alert"
        data-test="run-version-hint"
      />

      <p class="editor__dialog-hint">
        输入必须是 JSON 对象，留空等同于 <code>{}</code>。创建后状态为 pending，
        执行结果请在运行追踪页查看；本页不会轮询进度，也不会生成 trace_id。
      </p>

      <el-input
        v-model="runInputText"
        type="textarea"
        :rows="6"
        data-test="run-input"
        placeholder='{"camera": "cam-1"}'
      />
      <p v-if="runInputError" class="editor__dialog-error" data-test="run-input-error">
        {{ runInputError }}
      </p>
      <p v-if="runError" class="editor__dialog-error" data-test="run-error">{{ runError }}</p>

      <div v-if="runResult" class="editor__run-result" data-test="run-result">
        <p>已创建运行记录（{{ runResult.status }}），执行结果将在运行追踪页查看。</p>
        <p>
          运行 ID：{{ runResult.id }} · 状态：{{ runResult.status }} · 使用版本：
          v{{ runResult.workflow_version }}
        </p>
      </div>

      <template #footer>
        <el-button :disabled="running" @click="runDialogVisible = false">关闭</el-button>
        <el-button
          type="primary"
          data-test="submit-run"
          :loading="running"
          @click="submitRun"
        >
          创建运行
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.editor-page {
  width: 100%;
}

.editor-page__card {
  border-radius: 12px;
}

.editor {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.editor__toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid #eef1f6;
}

.editor__heading {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.editor__title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #111827;
}

.editor__meta {
  font-size: 12px;
  color: #6b7280;
}

.editor__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.editor__run-wrap {
  display: inline-flex;
}

.editor__notice {
  margin: 0;
}

.editor__run-hint {
  margin: 0;
  font-size: 12px;
  color: #b45309;
}

.editor__body {
  display: grid;
  grid-template-columns: 210px minmax(0, 1fr) 350px;
  gap: 14px;
  align-items: start;
}

.editor__palette {
  min-width: 0;
}

.editor__canvas {
  position: relative;
  height: 640px;
  overflow: hidden;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  background: #f8fafc;
}

.editor__flow {
  width: 100%;
  height: 100%;
}

.editor__canvas-empty {
  position: absolute;
  top: 16px;
  left: 50%;
  transform: translateX(-50%);
  padding: 8px 14px;
  border: 1px dashed #cbd5e1;
  border-radius: 999px;
  background: rgba(255, 255, 255, 0.9);
  font-size: 12px;
  color: #64748b;
  pointer-events: none;
}

.editor__side {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
  max-height: 640px;
  overflow-y: auto;
  padding-right: 2px;
}

.editor__dialog-alert {
  margin-bottom: 10px;
}

.editor__dialog-hint {
  margin: 0 0 10px;
  font-size: 12px;
  line-height: 1.7;
  color: #6b7280;
}

.editor__dialog-hint code {
  padding: 1px 4px;
  border-radius: 4px;
  background: #f3f4f6;
  color: #374151;
}

.editor__dialog-error {
  margin: 8px 0 0;
  font-size: 12px;
  color: #dc2626;
}

.editor__run-result {
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid #bfdbfe;
  border-radius: 10px;
  background: #eff6ff;
  font-size: 12px;
  line-height: 1.7;
  color: #1e3a8a;
}

.editor__run-result p {
  margin: 0;
}

.editor :deep(.vue-flow__node) {
  font-family: inherit;
}

.editor :deep(.vue-flow__edge-path) {
  stroke: #94a3b8;
  stroke-width: 1.5;
}

.editor :deep(.vue-flow__controls) {
  box-shadow: 0 4px 12px rgba(15, 23, 42, 0.12);
}

@media (max-width: 1280px) {
  .editor__body {
    grid-template-columns: 180px minmax(0, 1fr);
  }

  .editor__side {
    grid-column: 1 / -1;
    max-height: none;
    overflow: visible;
  }
}

@media (max-width: 1024px) {
  .editor__body {
    grid-template-columns: minmax(0, 1fr);
  }

  .editor__canvas {
    height: 520px;
  }
}
</style>

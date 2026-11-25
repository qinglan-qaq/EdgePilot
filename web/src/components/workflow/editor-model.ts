import type { Edge, Node, XYPosition } from '@vue-flow/core'

import type { WorkflowDsl, WorkflowEdgeDsl, WorkflowNodeDsl } from '@/api/workflows'

/** 工作流 DSL 支持的六种节点类型。 */
export type WorkflowNodeType = 'start' | 'agent' | 'tool' | 'condition' | 'approval' | 'end'

/** 节点面板与画布使用的固定顺序。 */
export const NODE_TYPE_ORDER: readonly WorkflowNodeType[] = [
  'start',
  'agent',
  'tool',
  'condition',
  'approval',
  'end',
]

/** 节点类型的展示元数据；badge 用于小色块中的文字标识，不依赖图标库。 */
export interface NodeTypeMeta {
  label: string
  badge: string
  color: string
  defaultTimeoutMs: number
  /** agent / tool 的默认重试次数，其余类型为 null。 */
  defaultMaxRetries: number | null
}

/** 普通节点默认超时 30 秒，审批节点默认超时 24 小时。 */
export const DEFAULT_NODE_TIMEOUT_MS = 30_000
export const DEFAULT_APPROVAL_TIMEOUT_MS = 86_400_000
export const MIN_TIMEOUT_MS = 1
export const MAX_TIMEOUT_MS = 86_400_000
export const DEFAULT_MAX_RETRIES = 2

/** 后端约定的节点 id 规则：字母开头，只含字母、数字、下划线，最长 64。 */
export const NODE_ID_PATTERN = /^[A-Za-z][A-Za-z0-9_]*$/
export const NODE_ID_MAX_LENGTH = 64
/** 保留节点 id，不允许用于表达式引用。 */
export const RESERVED_NODE_IDS: readonly string[] = ['input']

/** 审批人可选角色；admin 需要显式勾选，不会自动获得审批权。 */
export const APPROVER_ROLES = ['viewer', 'operator', 'admin'] as const
export type ApproverRole = (typeof APPROVER_ROLES)[number]

/** 拖拽节点时写入 DataTransfer 的自定义 MIME，避免与文本拖拽冲突。 */
export const WORKFLOW_NODE_DRAG_TYPE = 'application/x-edgepilot-workflow-node'

/** 各类型节点的中文名、颜色与默认值。 */
export const NODE_TYPE_META: Record<WorkflowNodeType, NodeTypeMeta> = {
  start: {
    label: '开始',
    badge: '入',
    color: '#10b981',
    defaultTimeoutMs: DEFAULT_NODE_TIMEOUT_MS,
    defaultMaxRetries: null,
  },
  agent: {
    label: 'Agent',
    badge: 'AI',
    color: '#2563eb',
    defaultTimeoutMs: DEFAULT_NODE_TIMEOUT_MS,
    defaultMaxRetries: DEFAULT_MAX_RETRIES,
  },
  tool: {
    label: '工具',
    badge: '具',
    color: '#7c3aed',
    defaultTimeoutMs: DEFAULT_NODE_TIMEOUT_MS,
    defaultMaxRetries: DEFAULT_MAX_RETRIES,
  },
  condition: {
    label: '条件',
    badge: '判',
    color: '#f59e0b',
    defaultTimeoutMs: DEFAULT_NODE_TIMEOUT_MS,
    defaultMaxRetries: null,
  },
  approval: {
    label: '审批',
    badge: '审',
    color: '#db2777',
    defaultTimeoutMs: DEFAULT_APPROVAL_TIMEOUT_MS,
    defaultMaxRetries: null,
  },
  end: {
    label: '结束',
    badge: '出',
    color: '#64748b',
    defaultTimeoutMs: DEFAULT_NODE_TIMEOUT_MS,
    defaultMaxRetries: null,
  },
}

/** Vue Flow 节点 data 中保存的编辑器模型。 */
export interface WorkflowNodeData {
  /** DSL 中的节点类型；允许为字符串，以兼容后端草稿里的异常类型。 */
  nodeType: string
  /** 已知业务字段（包括 timeout_ms），不含 id / type / position。 */
  fields: Record<string, unknown>
  /** DSL 中未知或未来新增字段，保存时原样写回。 */
  extra: Record<string, unknown>
}

/** 画布节点：Vue Flow Node 上强制存在 data，避免模板中反复判空。 */
export type FlowNode = Node<WorkflowNodeData> & { data: WorkflowNodeData }

/** 画布边的附加数据。 */
export interface FlowEdgeData {
  /** condition 分支标识；非 condition 出边必须为 undefined。 */
  when?: 'true' | 'false'
  /** DSL 中未知或未来新增字段，保存时原样写回。 */
  extra: Record<string, unknown>
}

/** 画布边：Vue Flow Edge 上强制存在 data。 */
export type FlowEdge = Edge<FlowEdgeData> & { data: FlowEdgeData }

/** 连线输入，与 Vue Flow @connect 事件结构一致。 */
export interface FlowConnectionInput {
  source: string
  target: string
  sourceHandle?: string | null
  targetHandle?: string | null
  /** 校验已存在的边时可传入其 id，避免把自身误判为重复边。 */
  id?: string | null
}

/** 连线结果；error 非空时 edges 保持原样。 */
export interface ConnectNodesResult {
  edges: FlowEdge[]
  error: string | null
}

/** 自动布局参数：从 start 出发按层级横向排布，同层纵向错开。 */
const LAYOUT_ORIGIN_X = 80
const LAYOUT_ORIGIN_Y = 80
const LAYOUT_COLUMN_WIDTH = 280
const LAYOUT_ROW_HEIGHT = 150

/** 所有类型共享的已知 DSL 字段。 */
const COMMON_NODE_FIELDS = new Set(['timeout_ms'])

/** 各类型独有的已知 DSL 字段。 */
const TYPE_NODE_FIELDS: Record<WorkflowNodeType, ReadonlySet<string>> = {
  start: new Set(),
  agent: new Set(['agent', 'input_map', 'max_retries']),
  tool: new Set(['tool', 'input_map', 'max_retries']),
  condition: new Set(['expression']),
  approval: new Set(['title', 'approvers']),
  end: new Set(),
}

/** 已知的边字段；其余字段进入 data.extra 以支持未来扩展。 */
const KNOWN_EDGE_FIELDS = new Set(['id', 'from', 'to', 'source', 'target', 'when'])

/** 判断任意值是否为受支持的节点类型。 */
export function isWorkflowNodeType(value: unknown): value is WorkflowNodeType {
  return typeof value === 'string' && (NODE_TYPE_ORDER as readonly string[]).includes(value)
}

/** 读取节点类型元数据；未知类型返回 null，调用方负责降级展示。 */
export function nodeTypeMeta(type: string): NodeTypeMeta | null {
  return isWorkflowNodeType(type) ? NODE_TYPE_META[type] : null
}

/** 生成新建节点的默认已知字段。 */
export function defaultFieldsFor(type: WorkflowNodeType): Record<string, unknown> {
  const meta = NODE_TYPE_META[type]
  const fields: Record<string, unknown> = { timeout_ms: meta.defaultTimeoutMs }
  if (type === 'agent') {
    fields.agent = ''
    fields.max_retries = DEFAULT_MAX_RETRIES
  } else if (type === 'tool') {
    fields.tool = ''
    fields.max_retries = DEFAULT_MAX_RETRIES
  } else if (type === 'condition') {
    fields.expression = ''
  } else if (type === 'approval') {
    fields.title = ''
    fields.approvers = []
  }
  return fields
}

/** 根据已有节点 id 计算下一个唯一 id；start 已存在时返回 null。 */
function nextNodeId(type: WorkflowNodeType, usedIds: Set<string>): string | null {
  if (type === 'start') {
    return usedIds.has('start') ? null : 'start'
  }
  if (type === 'end') {
    if (!usedIds.has('end')) {
      return 'end'
    }
    let index = 2
    while (usedIds.has(`end_${index}`)) {
      index += 1
    }
    return `end_${index}`
  }
  let index = 1
  while (usedIds.has(`${type}_${index}`)) {
    index += 1
  }
  return `${type}_${index}`
}

/**
 * 创建画布节点。
 * start 已存在时返回 null（UI 层负责提示“只能有一个开始节点”）；
 * 其余类型按 agent_1 / end / end_2 规则递增生成唯一 id。
 */
export function createFlowNode(
  type: WorkflowNodeType,
  existingNodes: readonly Pick<FlowNode, 'id' | 'data'>[],
  position: XYPosition,
): FlowNode | null {
  const usedIds = new Set(existingNodes.map((node) => node.id))
  const hasStart =
    usedIds.has('start') || existingNodes.some((node) => node.data?.nodeType === 'start')
  if (type === 'start' && hasStart) {
    return null
  }

  const id = nextNodeId(type, usedIds)
  if (id === null) {
    return null
  }

  return {
    id,
    type: 'workflow',
    position: { x: position.x, y: position.y },
    data: {
      nodeType: type,
      fields: defaultFieldsFor(type),
      extra: {},
    },
  }
}

/** 判断对象是否可作为 JSON 对象读取。 */
function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return null
  }
  return value as Record<string, unknown>
}

/** 读取合法的 position；缺失或非有限数值时返回 null。 */
function readPosition(value: unknown): XYPosition | null {
  const record = asRecord(value)
  if (!record) {
    return null
  }
  const x = record.x
  const y = record.y
  if (typeof x !== 'number' || !Number.isFinite(x) || typeof y !== 'number' || !Number.isFinite(y)) {
    return null
  }
  return { x, y }
}

/** 计算从 start 出发的层级布局；断开节点统一放在最深层，保持确定性。 */
function computeAutoLayout(
  nodes: readonly { id: string; nodeType: string }[],
  pairs: readonly { source: string; target: string }[],
): Map<string, XYPosition> {
  const knownIds = new Set(nodes.map((node) => node.id))
  const adjacency = new Map<string, string[]>()
  for (const node of nodes) {
    adjacency.set(node.id, [])
  }
  for (const pair of pairs) {
    if (knownIds.has(pair.source) && knownIds.has(pair.target)) {
      adjacency.get(pair.source)?.push(pair.target)
    }
  }

  const start = nodes.find((node) => node.nodeType === 'start') ?? nodes[0]
  const depth = new Map<string, number>()
  const queue: string[] = []
  if (start) {
    depth.set(start.id, 0)
    queue.push(start.id)
  }

  let head = 0
  while (head < queue.length) {
    const current = queue[head]
    head += 1
    const currentDepth = depth.get(current) ?? 0
    for (const next of adjacency.get(current) ?? []) {
      if (!depth.has(next)) {
        depth.set(next, currentDepth + 1)
        queue.push(next)
      }
    }
  }

  let maxDepth = -1
  for (const value of depth.values()) {
    maxDepth = Math.max(maxDepth, value)
  }
  const fallbackDepth = maxDepth + 1
  for (const node of nodes) {
    if (!depth.has(node.id)) {
      depth.set(node.id, fallbackDepth)
    }
  }

  const rowCounters = new Map<number, number>()
  const positions = new Map<string, XYPosition>()
  for (const node of nodes) {
    const level = depth.get(node.id) ?? 0
    const row = rowCounters.get(level) ?? 0
    rowCounters.set(level, row + 1)
    positions.set(node.id, {
      x: LAYOUT_ORIGIN_X + level * LAYOUT_COLUMN_WIDTH,
      y: LAYOUT_ORIGIN_Y + row * LAYOUT_ROW_HEIGHT,
    })
  }
  return positions
}

/** 读取节点 DSL 中的边端点，兼容后端使用的 from/to 与编辑器侧常见的 source/target。 */
function readEdgeEndpoints(edge: WorkflowEdgeDsl): { source: string; target: string } | null {
  const source = edge.from ?? edge.source
  const target = edge.to ?? edge.target
  if (typeof source !== 'string' || typeof target !== 'string') {
    return null
  }
  return { source, target }
}

/** 生成唯一边 id。 */
function uniqueEdgeId(base: string, usedIds: Set<string>): string {
  if (!usedIds.has(base)) {
    return base
  }
  let index = 2
  while (usedIds.has(`${base}_${index}`)) {
    index += 1
  }
  return `${base}_${index}`
}

/**
 * 把后端 DSL 转为 Vue Flow 的节点与边。
 * - 节点保留 position 与未知字段（未知字段放入 data.extra）；
 * - 缺失 position 的节点按 start 出发的层级做确定性自动布局；
 * - 兼容 from/to 与 source/target 两种边字段写法。
 */
export function dslToFlow(dsl: WorkflowDsl | null | undefined): {
  nodes: FlowNode[]
  edges: FlowEdge[]
} {
  const rawNodes = Array.isArray(dsl?.nodes) ? dsl.nodes : []
  const rawEdges = Array.isArray(dsl?.edges) ? dsl.edges : []

  const parsedNodes: Array<{
    raw: WorkflowNodeDsl
    node: Omit<FlowNode, 'position'> & { position: XYPosition | null }
  }> = []
  const usedIds = new Set<string>()

  rawNodes.forEach((entry, index) => {
    const raw = asRecord(entry)
    if (!raw) {
      return
    }
    let id = typeof raw.id === 'string' && raw.id ? raw.id : `node_${index + 1}`
    while (usedIds.has(id)) {
      id = `${id}_${usedIds.size + 1}`
    }
    usedIds.add(id)

    const rawType = typeof raw.type === 'string' && raw.type ? raw.type : 'agent'
    const nodeType = rawType
    const knownFields = isWorkflowNodeType(nodeType)
      ? new Set([...COMMON_NODE_FIELDS, ...TYPE_NODE_FIELDS[nodeType]])
      : COMMON_NODE_FIELDS

    const fields: Record<string, unknown> = {}
    const extra: Record<string, unknown> = {}
    for (const [key, value] of Object.entries(raw)) {
      if (key === 'id' || key === 'type' || key === 'position') {
        continue
      }
      if (knownFields.has(key)) {
        fields[key] = value
      } else {
        extra[key] = value
      }
    }

    parsedNodes.push({
      raw,
      node: {
        id,
        type: 'workflow',
        data: { nodeType, fields, extra },
        position: readPosition(raw.position),
      },
    })
  })

  const pairs: Array<{ source: string; target: string }> = []
  const edgeEntries: Array<{
    id: string
    source: string
    target: string
    when?: 'true' | 'false'
    extra: Record<string, unknown>
  }> = []
  const usedEdgeIds = new Set<string>()

  rawEdges.forEach((entry) => {
    const raw = asRecord(entry)
    if (!raw) {
      return
    }
    const endpoints = readEdgeEndpoints(raw as WorkflowEdgeDsl)
    if (!endpoints) {
      return
    }
    pairs.push(endpoints)

    const when = raw.when === 'true' || raw.when === 'false' ? raw.when : undefined
    const extra: Record<string, unknown> = {}
    for (const [key, value] of Object.entries(raw)) {
      if (!KNOWN_EDGE_FIELDS.has(key)) {
        extra[key] = value
      }
    }
    if (raw.when !== undefined && when === undefined) {
      extra.when = raw.when
    }

    const baseId =
      typeof raw.id === 'string' && raw.id
        ? raw.id
        : `edge_${endpoints.source}_${endpoints.target}${when ? `_${when}` : ''}`
    const id = uniqueEdgeId(baseId, usedEdgeIds)
    usedEdgeIds.add(id)
    edgeEntries.push({ ...endpoints, id, when, extra })
  })

  const layout = computeAutoLayout(
    parsedNodes.map(({ node }) => ({ id: node.id, nodeType: node.data.nodeType })),
    pairs,
  )

  const nodes: FlowNode[] = parsedNodes.map(({ node }) => ({
    ...node,
    position: node.position ?? layout.get(node.id) ?? { x: LAYOUT_ORIGIN_X, y: LAYOUT_ORIGIN_Y },
  }))

  const edges: FlowEdge[] = edgeEntries.map((entry) => ({
    id: entry.id,
    source: entry.source,
    target: entry.target,
    sourceHandle: entry.when,
    data: { when: entry.when, extra: entry.extra },
  }))

  return { nodes, edges }
}

/** 节点 position 取整，避免拖动产生的亚像素值让草稿出现无意义的噪声。 */
function roundPosition(position: XYPosition | undefined): XYPosition {
  const x = position?.x
  const y = position?.y
  return {
    x: typeof x === 'number' && Number.isFinite(x) ? Math.round(x) : 0,
    y: typeof y === 'number' && Number.isFinite(y) ? Math.round(y) : 0,
  }
}

/**
 * 把画布节点与边还原为后端 DSL。
 * 边使用后端实际读取的 from/to 字段；condition 出边额外带 when。
 */
export function flowToDsl(nodes: readonly FlowNode[], edges: readonly FlowEdge[]): WorkflowDsl {
  const dslNodes: WorkflowNodeDsl[] = nodes.map((node) => ({
    id: node.id,
    type: node.data?.nodeType,
    ...(node.data?.fields ?? {}),
    ...(node.data?.extra ?? {}),
    position: roundPosition(node.position),
  }))

  const dslEdges: WorkflowEdgeDsl[] = edges.map((edge) => {
    const when = edge.data?.when
    return {
      id: edge.id,
      from: edge.source,
      to: edge.target,
      ...(when ? { when } : {}),
      ...(edge.data?.extra ?? {}),
    }
  })

  return { nodes: dslNodes, edges: dslEdges }
}

/**
 * 处理一次连线。
 * - condition 节点必须从 true/false 分支发起，并替换同分支旧边；
 * - 非 condition 节点拒绝重复边、自环、start 入边与 end 出边；
 * - 生成唯一 edge id。
 */
export function connectFlowNodes(
  nodes: readonly FlowNode[],
  edges: readonly FlowEdge[],
  connection: FlowConnectionInput,
): ConnectNodesResult {
  const { source, target } = connection
  const sourceNode = nodes.find((node) => node.id === source)
  const targetNode = nodes.find((node) => node.id === target)
  // Vue Flow 校验已有边时会把该边本身放进 edges；先排除它再判断重复。
  const existing = connection.id
    ? edges.filter((edge) => edge.id !== connection.id)
    : [...edges]

  if (!sourceNode || !targetNode) {
    return { edges: existing, error: '连线两端的节点必须存在' }
  }
  if (source === target) {
    return { edges: existing, error: '节点不允许连接自身' }
  }
  if (sourceNode.data.nodeType === 'end') {
    return { edges: existing, error: 'end 节点不允许有出边' }
  }
  if (targetNode.data.nodeType === 'start') {
    return { edges: existing, error: 'start 节点不允许有入边' }
  }

  const isCondition = sourceNode.data.nodeType === 'condition'
  const branch = connection.sourceHandle
  if (isCondition && branch !== 'true' && branch !== 'false') {
    return { edges: existing, error: 'condition 节点必须从“是”或“否”分支发起连线' }
  }
  if (!isCondition && (branch === 'true' || branch === 'false')) {
    return { edges: existing, error: '只有 condition 节点才能使用分支连线' }
  }

  let remaining: FlowEdge[]
  if (isCondition) {
    const when = branch as 'true' | 'false'
    remaining = existing.filter((edge) => !(edge.source === source && edgeSourceWhen(edge) === when))
    if (remaining.some((edge) => edge.source === source && edge.target === target)) {
      return { edges: existing, error: 'condition 的两个分支不能指向同一节点' }
    }
    const usedIds = new Set(remaining.map((edge) => edge.id))
    const id = uniqueEdgeId(`edge_${source}_${target}_${when}`, usedIds)
    const nextEdge: FlowEdge = {
      id,
      source,
      target,
      sourceHandle: when,
      data: { when, extra: {} },
    }
    return { edges: [...remaining, nextEdge], error: null }
  }

  if (existing.some((edge) => edge.source === source && edge.target === target)) {
    return { edges: existing, error: '该连线已存在' }
  }

  const usedIds = new Set(existing.map((edge) => edge.id))
  const id = uniqueEdgeId(`edge_${source}_${target}`, usedIds)
  const nextEdge: FlowEdge = {
    id,
    source,
    target,
    data: { extra: {} },
  }
  return { edges: [...existing, nextEdge], error: null }
}

/** 读取边上的 when，兼容 data.when 与 sourceHandle 两种存放方式。 */
function edgeSourceWhen(edge: FlowEdge): 'true' | 'false' | undefined {
  if (edge.data?.when === 'true' || edge.data?.when === 'false') {
    return edge.data.when
  }
  if (edge.sourceHandle === 'true' || edge.sourceHandle === 'false') {
    return edge.sourceHandle
  }
  return undefined
}

/** 稳定 JSON 序列化：对象键排序，忽略 undefined，用于 dirty 对比。 */
export function stableStringify(value: unknown): string {
  if (value === null || typeof value !== 'object') {
    const json = JSON.stringify(value)
    return json === undefined ? 'null' : json
  }
  if (Array.isArray(value)) {
    return `[${value.map((item) => stableStringify(item)).join(',')}]`
  }
  const record = value as Record<string, unknown>
  const parts = Object.keys(record)
    .sort()
    .filter((key) => record[key] !== undefined)
    .map((key) => `${JSON.stringify(key)}:${stableStringify(record[key])}`)
  return `{${parts.join(',')}}`
}

/** 把超时毫秒格式化为便于阅读的中文文案。 */
export function formatTimeout(value: unknown): string {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) {
    return '未设置'
  }
  if (value % 3_600_000 === 0) {
    return `${value / 3_600_000} 小时`
  }
  if (value % 60_000 === 0) {
    return `${value / 60_000} 分钟`
  }
  if (value % 1_000 === 0) {
    return `${value / 1_000} 秒`
  }
  return `${value} 毫秒`
}

/** 输入映射读取时使用的 JSON Schema 属性视图。 */
export interface JsonSchemaProperty {
  type?: string
  title?: string
  description?: string
  [key: string]: unknown
}

/** 精简后的 JSON Schema 视图，仅保留参数映射需要的字段。 */
export interface JsonSchemaView {
  properties: Record<string, JsonSchemaProperty>
  required: string[]
}

/** 从 Agent config / Tool 定义中提取可用的 input_schema。 */
export function extractInputSchema(value: unknown): JsonSchemaView | null {
  const record = asRecord(value)
  if (!record) {
    return null
  }
  const rawProperties = asRecord(record.properties)
  if (!rawProperties) {
    return null
  }
  const properties: Record<string, JsonSchemaProperty> = {}
  for (const [key, rawProperty] of Object.entries(rawProperties)) {
    const property = asRecord(rawProperty)
    properties[key] = property ? (property as JsonSchemaProperty) : {}
  }
  if (Object.keys(properties).length === 0) {
    return null
  }
  const required = Array.isArray(record.required)
    ? record.required.filter((item): item is string => typeof item === 'string')
    : []
  return { properties, required }
}

/** 根据 schema 类型生成新增映射的默认值。 */
export function defaultInputValue(type: string | undefined): unknown {
  if (type === 'number' || type === 'integer') {
    return 0
  }
  if (type === 'boolean') {
    return false
  }
  return ''
}

/** 把输入框中的文本按 schema 类型转换；以 $ 开头的值始终保留为表达式字符串。 */
export function coerceInputValue(raw: string, type: string | undefined): unknown {
  if (raw.startsWith('$')) {
    return raw
  }
  if (type === 'number') {
    const parsed = Number(raw)
    return raw.trim() !== '' && Number.isFinite(parsed) ? parsed : raw
  }
  if (type === 'integer') {
    const parsed = Number(raw)
    return raw.trim() !== '' && Number.isFinite(parsed) ? Math.trunc(parsed) : raw
  }
  if (type === 'boolean') {
    const normalized = raw.trim().toLowerCase()
    if (normalized === 'true' || normalized === '1') {
      return true
    }
    if (normalized === 'false' || normalized === '0') {
      return false
    }
    return raw
  }
  return raw
}

/** 把 input_map 中的标量值展示为输入框文本。 */
export function displayInputValue(value: unknown): string {
  if (value === undefined || value === null) {
    return ''
  }
  if (typeof value === 'string') {
    return value
  }
  if (typeof value === 'number' || typeof value === 'boolean') {
    return String(value)
  }
  return JSON.stringify(value)
}

/** 计算某个节点的全部上游节点 id，保持节点列表中的声明顺序。 */
export function upstreamNodeIds(
  targetId: string,
  nodes: readonly { id: string }[],
  edges: readonly { source: string; target: string }[],
): string[] {
  const reverse = new Map<string, string[]>()
  for (const edge of edges) {
    const sources = reverse.get(edge.target) ?? []
    sources.push(edge.source)
    reverse.set(edge.target, sources)
  }

  const visited = new Set<string>()
  const stack = [...(reverse.get(targetId) ?? [])]
  while (stack.length > 0) {
    const current = stack.pop() as string
    if (visited.has(current)) {
      continue
    }
    visited.add(current)
    stack.push(...(reverse.get(current) ?? []))
  }

  return nodes.map((node) => node.id).filter((id) => visited.has(id))
}

import { api } from '@/api/http'

/** 工作流发布状态；后端当前只会返回 draft / published。 */
export type WorkflowStatus = 'draft' | 'published'

/** 运行触发方式。 */
export type WorkflowTriggerType = 'manual' | 'schedule' | 'event'

/** 列表接口中附带的“最近一次运行”摘要。 */
export interface LastRunSummary {
  id: number
  status: string
  created_at: string | null
}

/** 分页响应信封解包后的结构。 */
export interface PageResult<T> {
  items: T[]
  total: number
  page: number
  size: number
}

/** GET /workflows 的列表项。 */
export interface WorkflowListItem {
  id: number
  name: string
  description: string | null
  status: WorkflowStatus
  current_version: number
  created_at: string | null
  updated_at: string | null
  last_run: LastRunSummary | null
}

/** 工作流 DSL 中的单个节点；具体字段由节点类型决定，因此保留扩展键。 */
export interface WorkflowNodeDsl {
  id?: string
  type?: string
  [key: string]: unknown
}

/** 工作流 DSL 中的单条边。 */
export interface WorkflowEdgeDsl {
  id?: string
  source?: string
  target?: string
  [key: string]: unknown
}

/** 工作流草稿 DSL；nodes / edges 为必填骨架。 */
export interface WorkflowDsl {
  nodes: WorkflowNodeDsl[]
  edges: WorkflowEdgeDsl[]
  [key: string]: unknown
}

/** GET /workflows/{id} 的详情；dsl 为当前草稿（无草稿时回退已发布版本）。 */
export interface WorkflowDetail {
  id: number
  name: string
  description: string | null
  status: WorkflowStatus
  current_version: number
  draft_version: number | null
  dsl: WorkflowDsl
  created_by: number | null
  created_at: string | null
  updated_at: string | null
  last_run: LastRunSummary | null
}

/** 单条校验问题；node_id 为 null 表示图级或顶层问题。 */
export interface ValidationIssue {
  code: string
  message: string
  node_id: string | null
}

/** POST /workflows/{id}/validate 返回的校验报告；非法时仍为 200。 */
export interface ValidationReport {
  valid: boolean
  errors: ValidationIssue[]
  warnings: ValidationIssue[]
  node_count: number
  edge_count: number
  entry_node: string | null
  end_nodes: string[]
  topological_order: string[] | null
}

/** 已发布的工作流版本快照。 */
export interface WorkflowVersion {
  id: number
  workflow_id: number
  version: number
  dsl: WorkflowDsl
  dsl_hash: string
  created_by: number | null
  created_at: string | null
}

/** POST /workflows/{id}/runs 返回的运行详情；其余字段留给后续运行追踪页。 */
export interface WorkflowRunDetail {
  id: number
  workflow_id: number
  workflow_name: string
  workflow_version_id: number
  workflow_version: number
  status: string
  trigger_type: WorkflowTriggerType
  trigger_payload: Record<string, unknown> | null
  input: Record<string, unknown> | null
  output: unknown
  error: string | null
  trace_id: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
  [key: string]: unknown
}

/** Agent 选择器所需的最小字段集合。 */
export interface AgentPickerItem {
  id: number
  name: string
  agent_type: string
  status: string
  config: Record<string, unknown>
  current_version: number
}

/** 工具选择器所需的最小字段集合。 */
export interface ToolPickerItem {
  tool_id: string
  name: string
  description: string | null
  input_schema: Record<string, unknown> | null
  output_schema: Record<string, unknown> | null
  enabled: boolean
}

export interface WorkflowCreatePayload {
  name: string
  description?: string | null
  dsl?: WorkflowDsl
}

export interface WorkflowUpdatePayload {
  name?: string
  description?: string | null
  dsl?: WorkflowDsl
}

export interface WorkflowRunPayload {
  input?: Record<string, unknown>
  trigger_type?: WorkflowTriggerType
  trigger_payload?: Record<string, unknown>
}

export interface ListWorkflowsParams {
  page: number
  size: number
}

/** 拉取工作流分页列表；响应信封已由 api 客户端解包。 */
export function listWorkflows(params: ListWorkflowsParams): Promise<PageResult<WorkflowListItem>> {
  return api.get<PageResult<WorkflowListItem>>('/workflows', { params })
}

/** 读取工作流详情（包含当前草稿 DSL）。 */
export function getWorkflow(id: number): Promise<WorkflowDetail> {
  return api.get<WorkflowDetail>(`/workflows/${id}`)
}

/** 创建工作流；需要 operator / admin 角色。 */
export function createWorkflow(payload: WorkflowCreatePayload): Promise<WorkflowDetail> {
  return api.post<WorkflowDetail>('/workflows', payload)
}

/** 更新工作流元数据或草稿；需要 operator / admin 角色。 */
export function updateWorkflow(
  id: number,
  payload: WorkflowUpdatePayload,
): Promise<WorkflowDetail> {
  return api.patch<WorkflowDetail>(`/workflows/${id}`, payload)
}

/** 校验草稿 DSL；不传 dsl 时校验后端保存的草稿。非法报告同样返回 200。 */
export function validateWorkflow(id: number, dsl?: WorkflowDsl): Promise<ValidationReport> {
  return api.post<ValidationReport>(`/workflows/${id}/validate`, dsl === undefined ? {} : { dsl })
}

/** 发布当前草稿；仅 admin 可用，草稿非法时后端返回 400。 */
export function publishWorkflow(id: number): Promise<WorkflowVersion> {
  return api.post<WorkflowVersion>(`/workflows/${id}/publish`)
}

/** 列出工作流的全部已发布版本。 */
export function listWorkflowVersions(id: number): Promise<WorkflowVersion[]> {
  return api.get<WorkflowVersion[]>(`/workflows/${id}/versions`)
}

/** 创建一次 pending 运行记录；需要 operator / admin 角色。 */
export function createWorkflowRun(
  id: number,
  payload: WorkflowRunPayload = {},
): Promise<WorkflowRunDetail> {
  return api.post<WorkflowRunDetail>(`/workflows/${id}/runs`, payload)
}

/** 供编辑器节点选择器使用的 Agent 选项，固定取前 100 条。 */
export async function listAgentsForPicker(): Promise<AgentPickerItem[]> {
  const result = await api.get<PageResult<AgentPickerItem>>('/agents', {
    params: { page: 1, size: 100 },
  })
  return result.items
}

/** 供编辑器节点选择器使用的工具选项。 */
export async function listToolsForPicker(): Promise<ToolPickerItem[]> {
  const result = await api.get<{ items: ToolPickerItem[]; total: number }>('/tools')
  return result.items
}

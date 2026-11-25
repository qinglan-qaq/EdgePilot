import { api } from '@/api/http'
import type { PageResult } from '@/api/types'

/** 已知运行状态；后端新增状态时保留字符串兜底，页面按原样展示。 */
export type RunStatus =
  | 'pending'
  | 'running'
  | 'waiting_approval'
  | 'succeeded'
  | 'failed'
  | 'timeout'
  | 'cancelled'
  | (string & {})

/** 已知节点运行状态；同样保留字符串兜底。 */
export type NodeStatus =
  | 'pending'
  | 'running'
  | 'waiting_approval'
  | 'succeeded'
  | 'failed'
  | 'skipped'
  | (string & {})

/** 节点审批信息；字段由后端按节点实际状态写入，缺省时不做推断。 */
export interface RunApproval {
  title?: string | null
  approvers?: string[] | null
  created_at?: string | null
  expires_at?: string | null
  decision?: string | null
  by?: number | string | null
  at?: string | null
  comment?: string | null
  [key: string]: unknown
}

/** GET /runs 列表项。 */
export interface WorkflowRun {
  id: number
  workflow_id: number
  workflow_name: string
  workflow_version_id: number
  workflow_version: number
  status: RunStatus
  trigger_type: string
  trigger_payload: Record<string, unknown> | null
  input: Record<string, unknown> | null
  output: unknown
  error: string | null
  trace_id: string | null
  /** 后端详情会返回；旧版本或列表响应可能缺省。 */
  root_span_id?: string | null
  created_at: string | null
  started_at: string | null
  finished_at: string | null
}

/** 运行详情中的节点时间线条目。 */
export interface WorkflowNodeRun {
  id: number
  run_id: number
  node_id: string
  node_type: string
  status: NodeStatus
  attempt: number
  input: Record<string, unknown> | null
  output: unknown
  approval: RunApproval | null
  error: string | null
  span_id: string | null
  heartbeat_at: string | null
  started_at: string | null
  finished_at: string | null
}

/**
 * GET /runs/{id} 的运行详情。
 * nodes 在详情接口中始终存在；声明为可选是为了兼容既有编辑器仅创建运行时的
 * 返回类型，页面侧统一按 `?? []` 处理。
 */
export interface WorkflowRunDetail extends WorkflowRun {
  nodes?: WorkflowNodeRun[]
}

export interface ListRunsParams {
  page: number
  size: number
  workflow_id?: number
  status?: RunStatus
}

/** 审批请求体：comment 为空时传空对象，避免写入空白字符串。 */
function approvalBody(comment?: string | null): { comment?: string } {
  const text = comment?.trim()
  return text ? { comment: text } : {}
}

/** 拉取运行分页列表；调用方负责省略值为空的筛选参数。 */
export function listRuns(params: ListRunsParams): Promise<PageResult<WorkflowRun>> {
  return api.get<PageResult<WorkflowRun>>('/runs', { params })
}

/** 读取运行详情（含节点时间线）。 */
export function getRun(id: number): Promise<WorkflowRunDetail> {
  return api.get<WorkflowRunDetail>(`/runs/${encodeURIComponent(String(id))}`)
}

/** 取消运行；仅 operator / admin 可用。 */
export function cancelRun(id: number): Promise<WorkflowRunDetail> {
  return api.post<WorkflowRunDetail>(`/runs/${encodeURIComponent(String(id))}/cancel`, {})
}

/** 批准当前等待中的审批节点；后端校验当前角色是否在 approvers 中。 */
export function approveRun(id: number, comment?: string | null): Promise<WorkflowRunDetail> {
  return api.post<WorkflowRunDetail>(
    `/runs/${encodeURIComponent(String(id))}/approve`,
    approvalBody(comment),
  )
}

/** 拒绝当前等待中的审批节点；后端校验当前角色是否在 approvers 中。 */
export function rejectRun(id: number, comment?: string | null): Promise<WorkflowRunDetail> {
  return api.post<WorkflowRunDetail>(
    `/runs/${encodeURIComponent(String(id))}/reject`,
    approvalBody(comment),
  )
}

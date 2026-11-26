import { api } from '@/api/http'

/**
 * Vision Center API（后端 T16/T18）。
 *
 * 所有路径都在网关的 `/vision` 前缀下；读接口任意登录角色可用，写接口
 * （上传 / 取消 / 重试 / 触发 / 研判）由后端限 admin/operator。
 * 结果帧不会在前端重新计算，`results` 原样返回 artifact 中的帧级检测。
 */

/** 检测框使用视频像素坐标系（左上/右下）。 */
export interface VisionBBox {
  x1: number
  y1: number
  x2: number
  y2: number
}

/** artifact 中的单个检测目标。 */
export interface VisionDetection {
  detection_id: string
  class_id: number
  class_name: string
  confidence: number
  bbox: VisionBBox
}

/** artifact 中的单个采样帧；后端按 timestamp_ms 升序返回。 */
export interface VisionFrame {
  frame_id: number
  timestamp_ms: number
  inference_ms?: number | null
  detections: VisionDetection[]
}

/** 任务状态的终态集合；其余状态继续轮询。 */
export type VisionTaskStatus =
  | 'pending'
  | 'running'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | (string & {})

/** GET /vision/tasks 与写接口返回的任务行。 */
export interface VisionTask {
  id: number
  task_uid: string
  status: VisionTaskStatus
  progress: number
  dispatch_attempts: number
  video_filename: string
  file_size_bytes: number
  conf_threshold: number
  frame_stride: number
  target_samples: number
  video_fps: number | null
  video_duration_ms: number | null
  video_width: number | null
  video_height: number | null
  frame_count: number | null
  sampled_every_n_frames: number | null
  model_name: string | null
  model_version: string | null
  device: string | null
  processed_frames: number | null
  detection_count: number
  event_count: number
  inference_ms: number | null
  error_code: string | null
  error_message: string | null
  created_at: string | null
  finished_at: string | null
}

/** 任务结果行：联动、Agent 研判与 artifact 元数据。 */
export interface VisionResult {
  task_uid: string
  frame_count: number | null
  detection_count: number
  class_counts: Record<string, number>
  peak_confidence: number | null
  inference_ms: number | null
  event_count: number
  summary: Record<string, unknown> | null
  workflow_run_id: number | null
  trigger_status: string
  trigger_error: string | null
  agent_run_id: number | null
  agent_event_id: number | null
  agent_status: string
  agent_error: string | null
  agent_trace_id: string | null
  artifact_path: string | null
  artifact_bytes: number | null
  artifact_file_sha256: string | null
  result_sha256: string | null
  result_schema_version: string | null
  purged_at: string | null
}

/** GET /vision/tasks/{uid} 的详情：任务字段 + 媒体状态 + 结果汇总。 */
export interface VisionTaskDetail extends VisionTask {
  has_video: boolean
  video_missing: boolean
  artifact_purged: boolean
  video_url: string | null
  video_expires_at: string | null
  result: VisionResult | null
}

/** GET /vision/tasks 的分页响应。 */
export interface VisionTaskPage {
  items: VisionTask[]
  total: number
  page: number
  size: number
}

/** GET /vision/tasks 的查询参数；空值由调用方省略。 */
export interface VisionTaskListParams {
  page?: number
  size?: number
  status?: string
  q?: string
  start?: string
  end?: string
}

/** GET /vision/tasks/{uid}/results 的查询参数。 */
export interface VisionResultsParams {
  start_ms?: number
  end_ms?: number
  class_name?: string
  min_confidence?: number
  limit?: number
  offset?: number
}

/**
 * 结果接口一次取全 artifact 的帧上限（与后端 Query(le=2000) 对齐）。
 *
 * 后端默认 `limit=300`，而任务默认抽帧目标约 900 帧；若沿用默认值，
 * 播放器在第 300 帧之后会静默停更检测框。读全量时显式使用后台上限。
 */
export const FULL_RESULTS_LIMIT = 2000

/** GET /vision/tasks/{uid}/results 的响应：artifact 元数据 + 帧窗口。 */
export interface VisionResults {
  task_uid: string
  has_video?: boolean
  artifact_purged?: boolean
  video: {
    width?: number
    height?: number
    fps?: number
    duration_ms?: number
    sampled_every_n_frames?: number
    [key: string]: unknown
  }
  model: {
    model_name?: string
    model_version?: string
    [key: string]: unknown
  }
  summary: {
    processed_frames?: number
    detection_count?: number
    class_counts?: Record<string, number>
    peak_confidence?: number | null
    [key: string]: unknown
  }
  query: {
    start_ms?: number | null
    end_ms?: number | null
    class_name?: string | null
    min_confidence?: number | null
    limit?: number
    offset?: number
    total?: number
    [key: string]: unknown
  }
  frames: VisionFrame[]
}

/** GET /vision/tasks/{uid}/events 的事件行。 */
export interface VisionEvent {
  id: number
  event_type: string
  severity: string | null
  confidence: number | null
  occurred_at: string
  bbox: Record<string, number> | null
  payload: Record<string, unknown> | null
  dedup_key: string
}

/** GET /vision/tasks/{uid}/events 的分页响应。 */
export interface VisionEventPage {
  items: VisionEvent[]
  total: number
  page: number
  size: number
}

/** GET /vision/overview 的统计卡片数据。 */
export interface VisionOverview {
  tasks: { total?: number; completed?: number; running?: number; failed?: number }
  frames: { processed_total?: number }
  detections: { total?: number }
  events: { total?: number; unacked?: number }
  model: { status?: string; model_version?: string; device?: string } | null
  recent_tasks: VisionTask[]
}

/** GET /vision/health：不可达时仍是 200，只把 reachable 置 false。 */
export interface VisionHealth {
  enabled: boolean
  reachable: boolean
  service: Record<string, unknown> | null
  model:
    | {
        status?: string
        model_name?: string
        model_version?: string
        device?: string
        license?: string
        load_error?: string | null
        [key: string]: unknown
      }
    | null
  error: string | null
}

/** 模型页展示的 DB 登记行；live 为推理服务实时返回的同名模型。 */
export interface VisionModelRow {
  model_key: string
  display_name?: string | null
  provider?: string | null
  model_version?: string | null
  weights_filename?: string | null
  weights_sha256?: string | null
  task_type?: string | null
  classes?: unknown
  input_size?: unknown
  license?: string | null
  license_url?: string | null
  commercial_use_notice?: string | null
  status?: string | null
  live?: Record<string, unknown> | null
  [key: string]: unknown
}

/** GET /vision/models 的响应；同时兼容只有数组的旧形状。 */
export interface VisionModelsResponse {
  items: VisionModelRow[]
  /** 部分版本用 models 作为数组键，读取时一并兼容。 */
  models?: VisionModelRow[]
  count?: number
  live?: Record<string, unknown> | null
}

/** POST /vision/tasks 的响应；deduplicated 表示命中同文件幂等任务。 */
export interface VisionCreateTaskResult {
  task: VisionTask
  deduplicated: boolean
}

function taskPath(taskUid: string): string {
  return `/vision/tasks/${encodeURIComponent(taskUid)}`
}

export const visionApi = {
  /** 推理服务与模型健康状态。 */
  health(): Promise<VisionHealth> {
    return api.get<VisionHealth>('/vision/health')
  },

  /** 任务/帧/检测/事件统计与最近任务。 */
  overview(): Promise<VisionOverview> {
    return api.get<VisionOverview>('/vision/overview')
  },

  /** DB 模型登记 + 推理服务实时模型列表。 */
  models(): Promise<VisionModelsResponse | VisionModelRow[]> {
    return api.get<VisionModelsResponse | VisionModelRow[]>('/vision/models')
  },

  /** 分页读取识别任务；筛选与搜索交给后端。 */
  tasks(params: VisionTaskListParams = {}): Promise<VisionTaskPage> {
    return api.get<VisionTaskPage>('/vision/tasks', { params })
  },

  /** 上传视频创建任务；FormData 原样透传（包含 file 与可选参数）。 */
  createTask(formData: FormData): Promise<VisionCreateTaskResult> {
    return api.post<VisionCreateTaskResult>('/vision/tasks', formData)
  },

  /** 读取任务详情（含媒体签名 URL 与结果汇总）。 */
  task(taskUid: string): Promise<VisionTaskDetail> {
    return api.get<VisionTaskDetail>(taskPath(taskUid))
  },

  /**
   * 读取 artifact 帧级结果；清理或哈希不符时后端返回 404/409。
   *
   * 未显式传参时默认拉取全量帧（`FULL_RESULTS_LIMIT`），避免后端默认
   * `limit=300` 导致长视频覆盖层在第 300 帧后静默停更；显式筛选参数
   * 原样透传，不叠加默认值。
   */
  results(
    taskUid: string,
    params: VisionResultsParams = { limit: FULL_RESULTS_LIMIT },
  ): Promise<VisionResults> {
    return api.get<VisionResults>(`${taskPath(taskUid)}/results`, { params })
  },

  /** 分页读取任务产生的事件。 */
  events(
    taskUid: string,
    params: { page?: number; size?: number } = {},
  ): Promise<VisionEventPage> {
    return api.get<VisionEventPage>(`${taskPath(taskUid)}/events`, { params })
  },

  /** 取消任务（终态幂等）。 */
  cancel(taskUid: string): Promise<{ task: VisionTask }> {
    return api.post<{ task: VisionTask }>(`${taskPath(taskUid)}/cancel`)
  },

  /** 重试失败/取消的任务。 */
  retry(taskUid: string): Promise<{ task: VisionTask }> {
    return api.post<{ task: VisionTask }>(`${taskPath(taskUid)}/retry`)
  },

  /** 受控重触发工作流联动。 */
  trigger(taskUid: string): Promise<Record<string, unknown>> {
    return api.post<Record<string, unknown>>(`${taskPath(taskUid)}/trigger`)
  },

  /** 对任务的最新事件发起 Agent 研判。 */
  analyze(taskUid: string): Promise<Record<string, unknown>> {
    return api.post<Record<string, unknown>>(`${taskPath(taskUid)}/analyze`)
  },

  /** 对指定事件发起 Agent 研判。 */
  analyzeEvent(eventId: number): Promise<Record<string, unknown>> {
    return api.post<Record<string, unknown>>(
      `/vision/events/${encodeURIComponent(String(eventId))}/analyze`,
    )
  },
}

import { api } from '@/api/http'

/** GET /events/{event_id} 返回的视觉事件详情。 */
export interface EventDetail {
  id: number
  node_id: number | null
  device_id: number | null
  event_type: string
  severity: string | null
  occurred_at: string
  received_at: string | null
  confidence: number | null
  track_id: string | null
  snapshot_path: string | null
  has_snapshot: boolean
  bbox: Record<string, unknown> | null
  model_version: string | null
  payload: Record<string, unknown> | null
  dedup_key: string
  status: string
  created_at: string | null
}

/** 读取单条视觉事件；仅登录角色可读。 */
export function getEvent(id: number): Promise<EventDetail> {
  return api.get<EventDetail>(`/events/${encodeURIComponent(String(id))}`)
}

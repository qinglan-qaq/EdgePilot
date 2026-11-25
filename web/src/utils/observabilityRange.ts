/**
 * 分析页共用时间范围预设。
 *
 * 与 Overview 保持一致：近 24 小时不传参数，由后端解析为最近 24 小时；
 * 其它预设只传 ISO `start_from`，结束时间由后端取当前时刻。
 */

import type { AnalyticsQuery } from '@/api/observability'

export type RangePreset = '1h' | '24h' | '7d' | '30d'

export interface RangeOption {
  value: RangePreset
  label: string
}

export const RANGE_OPTIONS: RangeOption[] = [
  { value: '1h', label: '近 1 小时' },
  { value: '24h', label: '近 24 小时' },
  { value: '7d', label: '近 7 天' },
  { value: '30d', label: '近 30 天' },
]

const RANGE_HOURS: Record<Exclude<RangePreset, '24h'>, number> = {
  '1h': 1,
  '7d': 24 * 7,
  '30d': 24 * 30,
}

/** 把预设转换为后端查询参数；24h 返回空对象表示“后端默认最近 24 小时”。 */
export function rangeQuery(preset: RangePreset): AnalyticsQuery {
  if (preset === '24h') {
    return {}
  }
  return { start_from: new Date(Date.now() - RANGE_HOURS[preset] * 3600_000).toISOString() }
}

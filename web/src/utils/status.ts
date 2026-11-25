/**
 * 运行 / 节点状态与数值展示的统一工具。
 * 未知状态一律原样返回，避免把后端新增状态显示成空白。
 */

export type StatusTagType = 'success' | 'info' | 'warning' | 'danger' | undefined

const RUN_STATUS_LABELS: Record<string, string> = {
  pending: '等待中',
  running: '运行中',
  waiting_approval: '待审批',
  succeeded: '成功',
  failed: '失败',
  timeout: '超时',
  cancelled: '已取消',
}

const RUN_STATUS_TAG_TYPES: Record<string, StatusTagType> = {
  pending: 'info',
  running: 'warning',
  waiting_approval: 'warning',
  succeeded: 'success',
  failed: 'danger',
  timeout: 'danger',
  cancelled: 'info',
}

const NODE_STATUS_LABELS: Record<string, string> = {
  pending: '等待中',
  running: '运行中',
  waiting_approval: '待审批',
  succeeded: '成功',
  failed: '失败',
  skipped: '已跳过',
}

const NODE_STATUS_TAG_TYPES: Record<string, StatusTagType> = {
  pending: 'info',
  running: 'warning',
  waiting_approval: 'warning',
  succeeded: 'success',
  failed: 'danger',
  skipped: 'info',
}

const TRIGGER_TYPE_LABELS: Record<string, string> = {
  manual: '手动',
  schedule: '定时',
  event: '事件',
}

const NODE_TYPE_LABELS: Record<string, string> = {
  start: '开始',
  agent: '智能体',
  tool: '工具',
  condition: '条件',
  approval: '审批',
  end: '结束',
}

const TERMINAL_RUN_STATUSES = new Set(['succeeded', 'failed', 'timeout', 'cancelled'])

export function runStatusLabel(status: string): string {
  return RUN_STATUS_LABELS[status] ?? status
}

export function runStatusTagType(status: string): StatusTagType {
  return RUN_STATUS_TAG_TYPES[status] ?? 'info'
}

/** 终态运行不再轮询；未知状态按非终态处理。 */
export function isTerminalRunStatus(status: string): boolean {
  return TERMINAL_RUN_STATUSES.has(status)
}

export function nodeStatusLabel(status: string): string {
  return NODE_STATUS_LABELS[status] ?? status
}

export function nodeStatusTagType(status: string): StatusTagType {
  return NODE_STATUS_TAG_TYPES[status] ?? 'info'
}

export function formatTriggerType(type: string): string {
  return TRIGGER_TYPE_LABELS[type] ?? type
}

export function nodeTypeLabel(type: string): string {
  return NODE_TYPE_LABELS[type] ?? type
}

/** 毫秒耗时：不足 1 秒显示 ms，1 分钟内保留 1 位秒，更长保留 1 位分。 */
export function formatDurationMs(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—'
  }
  const ms = Math.max(0, value)
  if (ms < 1000) {
    return `${Math.round(ms)} ms`
  }
  if (ms < 60000) {
    return `${(ms / 1000).toFixed(1)} 秒`
  }
  return `${(ms / 60000).toFixed(1)} 分`
}

/**
 * 结束时间 - 开始时间。
 * 未结束且 live 为 true 时只显示“进行中”，不使用当前时间推算耗时。
 */
export function formatDurationBetween(
  start: string | null | undefined,
  end: string | null | undefined,
  options: { live?: boolean } = {},
): string {
  const startTime = parseTime(start)
  if (startTime === null) {
    return '—'
  }
  if (end === null || end === undefined || !String(end).trim()) {
    return options.live ? '进行中' : '—'
  }
  const endTime = parseTime(end)
  if (endTime === null || endTime < startTime) {
    return '—'
  }
  return formatDurationMs(endTime - startTime)
}

/** 无 token 数据时显示“未上报”，绝不用 0 冒充未知。 */
export function formatTokens(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '未上报'
  }
  return String(value)
}

/**
 * 金额展示：null / 非法值显示“未上报”，不做估算。
 * 数字或十进制字符串统一保留最多 6 位小数并去掉末尾 0。
 */
export function formatCost(value: string | number | null | undefined): string {
  if (value === null || value === undefined) {
    return '未上报'
  }
  if (typeof value === 'string' && !value.trim()) {
    return '未上报'
  }
  const numeric = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(numeric)) {
    return '未上报'
  }
  const fixed = numeric.toFixed(6)
  const trimmed = fixed.replace(/\.?0+$/, '')
  return trimmed === '' ? '0' : trimmed
}

function parseTime(value: string | null | undefined): number | null {
  if (value === null || value === undefined) {
    return null
  }
  const text = String(value).trim()
  if (!text) {
    return null
  }
  const timestamp = new Date(text).getTime()
  return Number.isNaN(timestamp) ? null : timestamp
}

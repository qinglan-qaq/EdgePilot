/** 空值占位符，列表与详情页统一使用。 */
const EMPTY_PLACEHOLDER = '—'

function pad(value: number): string {
  return String(value).padStart(2, '0')
}

/**
 * 把后端 ISO 时间格式化为本地时区的 `YYYY-MM-DD HH:mm`。
 * 空值、空白字符串或无法解析的时间统一返回 `—`，避免出现 Invalid Date。
 */
export function formatDateTime(value: string | null | undefined): string {
  if (value === null || value === undefined) {
    return EMPTY_PLACEHOLDER
  }

  const text = value.trim()
  if (!text) {
    return EMPTY_PLACEHOLDER
  }

  const date = new Date(text)
  if (Number.isNaN(date.getTime())) {
    return EMPTY_PLACEHOLDER
  }

  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours(),
  )}:${pad(date.getMinutes())}`
}

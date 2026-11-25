/**
 * 只允许跳转到站内路径，防止开放重定向。
 * 合法值必须以 "/" 开头且不能以 "//" 开头。
 */
export function sanitizeRedirect(value: unknown): string | null {
  if (typeof value !== 'string') {
    return null
  }
  if (!value.startsWith('/') || value.startsWith('//')) {
    return null
  }
  return value
}

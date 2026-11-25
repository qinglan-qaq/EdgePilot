import { describe, expect, it } from 'vitest'

import { formatDateTime } from '../src/utils/datetime'

describe('formatDateTime', () => {
  it('空值与空白字符串返回破折号', () => {
    expect(formatDateTime(null)).toBe('—')
    expect(formatDateTime(undefined)).toBe('—')
    expect(formatDateTime('')).toBe('—')
    expect(formatDateTime('   ')).toBe('—')
  })

  it('无法解析的时间返回破折号', () => {
    expect(formatDateTime('not-a-date')).toBe('—')
  })

  it('按本地时区输出 YYYY-MM-DD HH:mm 且补零', () => {
    // 用本地时间构造再转 ISO，避免断言依赖测试机器的时区。
    const local = new Date(2026, 0, 2, 3, 4, 5)
    expect(formatDateTime(local.toISOString())).toBe('2026-01-02 03:04')
  })

  it('保留后端不带时区的本地时间字符串', () => {
    expect(formatDateTime('2026-11-21T12:00:00')).toBe('2026-11-21 12:00')
  })
})

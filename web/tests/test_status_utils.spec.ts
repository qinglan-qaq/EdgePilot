import { describe, expect, it } from 'vitest'

import {
  formatCost,
  formatDurationBetween,
  formatDurationMs,
  formatTokens,
  formatTriggerType,
  isTerminalRunStatus,
  nodeStatusLabel,
  nodeStatusTagType,
  nodeTypeLabel,
  runStatusLabel,
  runStatusTagType,
} from '../src/utils/status'

describe('运行状态工具', () => {
  it('运行状态使用中文标签，未知状态原样返回', () => {
    expect(runStatusLabel('pending')).toBe('等待中')
    expect(runStatusLabel('running')).toBe('运行中')
    expect(runStatusLabel('waiting_approval')).toBe('待审批')
    expect(runStatusLabel('succeeded')).toBe('成功')
    expect(runStatusLabel('failed')).toBe('失败')
    expect(runStatusLabel('timeout')).toBe('超时')
    expect(runStatusLabel('cancelled')).toBe('已取消')
    expect(runStatusLabel('paused_by_operator')).toBe('paused_by_operator')
  })

  it('运行与节点状态的标签类型符合语义', () => {
    expect(runStatusTagType('succeeded')).toBe('success')
    expect(runStatusTagType('failed')).toBe('danger')
    expect(runStatusTagType('running')).toBe('warning')
    expect(runStatusTagType('mystery')).toBe('info')

    expect(nodeStatusLabel('skipped')).toBe('已跳过')
    expect(nodeStatusLabel('waiting_approval')).toBe('待审批')
    expect(nodeStatusLabel('mystery_node')).toBe('mystery_node')
    expect(nodeStatusTagType('failed')).toBe('danger')
    expect(nodeStatusTagType('skipped')).toBe('info')
  })

  it('节点类型使用中文标签，未知类型原样返回', () => {
    expect(nodeTypeLabel('start')).toBe('开始')
    expect(nodeTypeLabel('agent')).toBe('智能体')
    expect(nodeTypeLabel('tool')).toBe('工具')
    expect(nodeTypeLabel('condition')).toBe('条件')
    expect(nodeTypeLabel('approval')).toBe('审批')
    expect(nodeTypeLabel('end')).toBe('结束')
    expect(nodeTypeLabel('custom')).toBe('custom')
  })

  it('只有成功/失败/超时/取消属于终态', () => {
    expect(isTerminalRunStatus('succeeded')).toBe(true)
    expect(isTerminalRunStatus('failed')).toBe(true)
    expect(isTerminalRunStatus('timeout')).toBe(true)
    expect(isTerminalRunStatus('cancelled')).toBe(true)
    expect(isTerminalRunStatus('pending')).toBe(false)
    expect(isTerminalRunStatus('running')).toBe(false)
    expect(isTerminalRunStatus('waiting_approval')).toBe(false)
    expect(isTerminalRunStatus('unknown')).toBe(false)
  })

  it('触发方式使用中文标签，未知值原样返回', () => {
    expect(formatTriggerType('manual')).toBe('手动')
    expect(formatTriggerType('schedule')).toBe('定时')
    expect(formatTriggerType('event')).toBe('事件')
    expect(formatTriggerType('webhook')).toBe('webhook')
  })

  it('token 与费用缺数据时显示未上报，不估算', () => {
    expect(formatTokens(null)).toBe('未上报')
    expect(formatTokens(undefined)).toBe('未上报')
    expect(formatTokens(0)).toBe('0')
    expect(formatTokens(1234)).toBe('1234')

    expect(formatCost(null)).toBe('未上报')
    expect(formatCost(undefined)).toBe('未上报')
    expect(formatCost('')).toBe('未上报')
    expect(formatCost('not-a-number')).toBe('未上报')
    expect(formatCost(0)).toBe('0')
    expect(formatCost('0.001230')).toBe('0.00123')
    expect(formatCost('0.123456789')).toBe('0.123457')
  })

  it('耗时格式化区分毫秒、秒、分，并处理未结束运行', () => {
    expect(formatDurationMs(null)).toBe('—')
    expect(formatDurationMs(850)).toBe('850 ms')
    expect(formatDurationMs(1500)).toBe('1.5 秒')
    expect(formatDurationMs(90000)).toBe('1.5 分')

    expect(
      formatDurationBetween('2026-11-22T10:00:00Z', '2026-11-22T10:00:02Z'),
    ).toBe('2.0 秒')
    expect(formatDurationBetween('2026-11-22T10:00:00Z', null, { live: true })).toBe(
      '进行中',
    )
    expect(formatDurationBetween('2026-11-22T10:00:00Z', null)).toBe('—')
    expect(formatDurationBetween(null, null)).toBe('—')
    expect(
      formatDurationBetween('2026-11-22T10:00:05Z', '2026-11-22T10:00:00Z'),
    ).toBe('—')
  })
})

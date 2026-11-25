import { afterEach, describe, expect, it } from 'vitest'
import { enableAutoUnmount, mount } from '@vue/test-utils'

import PromptDiff from '../src/components/observability/PromptDiff.vue'

enableAutoUnmount(afterEach)

function mountDiff(left: string | null, right: string | null) {
  return mount(PromptDiff, {
    props: {
      left,
      right,
      leftLabel: 'v1',
      rightLabel: 'v2',
    },
  })
}

describe('PromptDiff', () => {
  it('按行做 LCS diff：相同行保持顺序，新增/删除行带对应 class', () => {
    const wrapper = mountDiff('alpha\nbeta\ngamma', 'alpha\ngamma\ndelta')

    const lines = wrapper.findAll('[data-test="diff-line"]')
    expect(lines.map((line) => line.attributes('data-kind'))).toEqual([
      'equal',
      'remove',
      'equal',
      'add',
    ])
    expect(
      lines.map((line) => line.find('.prompt-diff__text').text()),
    ).toEqual(['alpha', 'beta', 'gamma', 'delta'])

    const removed = wrapper.find('.prompt-diff__line--remove')
    expect(removed.exists()).toBe(true)
    expect(removed.text()).toContain('beta')

    const added = wrapper.find('.prompt-diff__line--add')
    expect(added.exists()).toBe(true)
    expect(added.text()).toContain('delta')

    // 相同行不得被标记为新增或删除。
    const equal = wrapper.findAll('.prompt-diff__line--equal')
    expect(equal.map((line) => line.text())).toEqual(['alpha', 'gamma'])
  })

  it('完全相同的内容不产生新增/删除行', () => {
    const wrapper = mountDiff('同一行\n第二行', '同一行\n第二行')

    expect(wrapper.findAll('.prompt-diff__line--add')).toHaveLength(0)
    expect(wrapper.findAll('.prompt-diff__line--remove')).toHaveLength(0)
    expect(wrapper.find('[data-test="prompt-diff-same"]').exists()).toBe(true)
  })

  it('null 版本显示“未记录”，不伪造内容', () => {
    const wrapper = mountDiff(null, null)

    expect(wrapper.find('[data-test="prompt-diff-empty"]').text()).toBe('未记录')
    expect(wrapper.findAll('[data-test="diff-line"]')).toHaveLength(0)
  })
})

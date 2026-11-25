import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'

import MarkdownBlock from '../src/components/common/MarkdownBlock.vue'

describe('MarkdownBlock', () => {
  it('渲染标题、列表与行内代码', () => {
    const wrapper = mount(MarkdownBlock, {
      props: {
        content: '# 巡检报告\n\n- 人员闯入\n- 车辆滞留\n\n详情见 `camera.md`。',
      },
    })

    const body = wrapper.find('[data-test="markdown-body"]')
    expect(body.exists()).toBe(true)
    expect(body.find('h1').text()).toBe('巡检报告')
    expect(body.findAll('li').map((item) => item.text())).toEqual([
      '人员闯入',
      '车辆滞留',
    ])
    expect(body.find('code').text()).toBe('camera.md')
  })

  it('过滤 script 与事件处理属性，避免 XSS', () => {
    const wrapper = mount(MarkdownBlock, {
      props: {
        content:
          '<script>window.__xss = 1</script>\n\n<img src="x" onerror="window.__xss = 2">\n\n[危险链接](javascript:alert(1))',
      },
    })

    const body = wrapper.find('[data-test="markdown-body"]')
    expect(body.find('script').exists()).toBe(false)
    expect(wrapper.html()).not.toContain('window.__xss')
    expect(wrapper.html()).not.toContain('onerror')

    const img = body.find('img')
    if (img.exists()) {
      expect(img.attributes('onerror')).toBeUndefined()
    }
    const link = body.find('a')
    if (link.exists()) {
      expect(link.attributes('href') ?? '').not.toContain('javascript:')
    }
  })

  it('空内容显示明确占位，不渲染空 HTML', () => {
    const wrapper = mount(MarkdownBlock, { props: { content: '   ' } })

    expect(wrapper.find('[data-test="markdown-body"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('无内容')
  })
})

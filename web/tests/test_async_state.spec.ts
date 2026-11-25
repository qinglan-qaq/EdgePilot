import { describe, expect, it } from 'vitest'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'

import AsyncState from '../src/components/common/AsyncState.vue'

const globalConfig = {
  plugins: [ElementPlus],
}

describe('AsyncState 三态组件', () => {
  it('loading 时渲染加载态', () => {
    const wrapper = mount(AsyncState, {
      props: { loading: true, error: null, empty: false },
      global: globalConfig,
      slots: { default: '<div>默认内容</div>' },
    })

    expect(wrapper.find('[data-test="async-loading"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('加载中')
    expect(wrapper.text()).not.toContain('默认内容')
  })

  it('error 时渲染错误信息并触发 retry 事件', async () => {
    const wrapper = mount(AsyncState, {
      props: { loading: false, error: '接口请求失败', empty: false },
      global: globalConfig,
      slots: { default: '<div>默认内容</div>' },
    })

    expect(wrapper.find('[data-test="async-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('接口请求失败')

    const retryButton = wrapper.findAll('button').find((button) => button.text().includes('重试'))
    expect(retryButton).toBeTruthy()
    await retryButton?.trigger('click')

    expect(wrapper.emitted('retry')).toHaveLength(1)
  })

  it('empty 时优先渲染 empty 插槽', () => {
    const wrapper = mount(AsyncState, {
      props: { loading: false, error: null, empty: true },
      global: globalConfig,
      slots: {
        default: '<div>默认内容</div>',
        empty: '<div data-test="custom-empty">暂无记录</div>',
      },
    })

    expect(wrapper.find('[data-test="async-empty"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="custom-empty"]').text()).toBe('暂无记录')
    expect(wrapper.text()).not.toContain('默认内容')
  })

  it('默认渲染内容插槽', () => {
    const wrapper = mount(AsyncState, {
      props: { loading: false, error: null, empty: false },
      global: globalConfig,
      slots: { default: '<div data-test="content">真实内容</div>' },
    })

    expect(wrapper.find('[data-test="async-content"]').exists()).toBe(true)
    expect(wrapper.find('[data-test="content"]').text()).toBe('真实内容')
  })
})

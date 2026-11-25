import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'

import type { UserRole } from '../src/api/http'
import type { WorkflowRun } from '../src/api/runs'

const authState = vi.hoisted(() => ({ role: 'operator' as UserRole }))
const routerPush = vi.hoisted(() => vi.fn())

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => authState,
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerPush }),
}))

vi.mock('@/api/runs', () => ({
  listRuns: vi.fn(),
}))

import { ApiError } from '../src/api/http'
import { listRuns } from '../src/api/runs'
import Runs from '../src/views/Runs.vue'

enableAutoUnmount(afterEach)

function makeRun(overrides: Partial<WorkflowRun> = {}): WorkflowRun {
  return {
    id: 11,
    workflow_id: 3,
    workflow_name: '河道巡检',
    workflow_version_id: 8,
    workflow_version: 2,
    status: 'succeeded',
    trigger_type: 'manual',
    trigger_payload: null,
    input: {},
    output: null,
    error: null,
    trace_id: null,
    root_span_id: null,
    created_at: '2026-11-21T12:00:00',
    started_at: '2026-11-21T12:00:10',
    finished_at: '2026-11-21T12:00:40',
    ...overrides,
  }
}

function mountPage() {
  return mount(Runs, {
    attachTo: document.body,
    global: {
      plugins: [ElementPlus],
    },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  authState.role = 'operator'
})

describe('运行列表页', () => {
  it('渲染真实运行字段，未采集 trace 与未开始耗时明确显示', async () => {
    vi.mocked(listRuns).mockResolvedValue({
      items: [
        makeRun(),
        makeRun({
          id: 12,
          workflow_name: '告警复核',
          status: 'pending',
          trigger_type: 'event',
          trace_id: null,
          started_at: null,
          finished_at: null,
          created_at: '2026-11-21T13:00:00',
        }),
      ],
      total: 2,
      page: 1,
      size: 20,
    })

    const wrapper = mountPage()
    await flushPromises()

    expect(listRuns).toHaveBeenCalledTimes(1)
    const params = vi.mocked(listRuns).mock.calls[0]?.[0] ?? {}
    expect(params).toEqual({ page: 1, size: 20 })
    expect('status' in params).toBe(false)

    const text = wrapper.text()
    expect(text).toContain('11')
    expect(text).toContain('河道巡检')
    expect(text).toContain('v2')
    expect(text).toContain('成功')
    expect(text).toContain('手动')
    expect(text).toContain('2026-11-21 12:00')
    expect(text).toContain('30.0 秒')
    expect(text).toContain('未采集')
    expect(text).toContain('等待中')
    expect(text).toContain('事件')
    expect(text).toContain('—')
  })

  it('状态筛选重置到第一页，并只在有值时才携带 status', async () => {
    vi.mocked(listRuns)
      .mockResolvedValueOnce({ items: [makeRun()], total: 45, page: 1, size: 20 })
      .mockResolvedValueOnce({ items: [makeRun()], total: 45, page: 2, size: 20 })
      .mockResolvedValue({ items: [makeRun({ status: 'failed' })], total: 1, page: 1, size: 20 })

    const wrapper = mountPage()
    await flushPromises()

    const page2 = wrapper.findAll('.el-pager li').find((item) => item.text() === '2')
    expect(page2).toBeTruthy()
    await page2?.trigger('click')
    await flushPromises()
    expect(listRuns).toHaveBeenLastCalledWith({ page: 2, size: 20 })

    const select = wrapper.findComponent({ name: 'ElSelect' })
    expect(select.exists()).toBe(true)
    select.vm.$emit('update:modelValue', 'failed')
    select.vm.$emit('change', 'failed')
    await flushPromises()

    expect(listRuns).toHaveBeenLastCalledWith({ page: 1, size: 20, status: 'failed' })
    expect(wrapper.text()).toContain('失败')

    select.vm.$emit('update:modelValue', '')
    select.vm.$emit('change', '')
    await flushPromises()

    const lastParams = vi.mocked(listRuns).mock.calls.at(-1)?.[0] ?? {}
    expect(lastParams).toEqual({ page: 1, size: 20 })
    expect('status' in lastParams).toBe(false)
  })

  it('翻页与刷新都按当前页码重新请求', async () => {
    vi.mocked(listRuns).mockResolvedValue({
      items: [makeRun()],
      total: 45,
      page: 1,
      size: 20,
    })

    const wrapper = mountPage()
    await flushPromises()

    const page3 = wrapper.findAll('.el-pager li').find((item) => item.text() === '3')
    expect(page3).toBeTruthy()
    await page3?.trigger('click')
    await flushPromises()
    expect(listRuns).toHaveBeenLastCalledWith({ page: 3, size: 20 })

    await wrapper.find('[data-test="runs-refresh"]').trigger('click')
    await flushPromises()
    expect(listRuns).toHaveBeenLastCalledWith({ page: 3, size: 20 })
    expect(listRuns).toHaveBeenCalledTimes(3)
  })

  it('点击行与“详情”都跳转运行详情路由', async () => {
    vi.mocked(listRuns).mockResolvedValue({
      items: [makeRun({ id: 31 })],
      total: 1,
      page: 1,
      size: 20,
    })

    const wrapper = mountPage()
    await flushPromises()

    await wrapper.find('.el-table__row').trigger('click')
    expect(routerPush).toHaveBeenCalledWith({
      name: 'run-detail',
      params: { id: 31 },
    })

    routerPush.mockClear()
    const detailButton = wrapper
      .findAll('button')
      .find((button) => button.text().trim() === '详情')
    expect(detailButton).toBeTruthy()
    await detailButton?.trigger('click')
    expect(routerPush).toHaveBeenCalledWith({
      name: 'run-detail',
      params: { id: 31 },
    })
  })

  it('加载失败展示后端错误，重试后渲染真实列表', async () => {
    vi.mocked(listRuns)
      .mockRejectedValueOnce(new ApiError(500, 500, '运行列表加载失败'))
      .mockResolvedValueOnce({
        items: [makeRun()],
        total: 1,
        page: 1,
        size: 20,
      })

    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('[data-test="async-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('运行列表加载失败')

    const retryButton = wrapper
      .findAll('button')
      .find((button) => button.text().trim() === '重试')
    expect(retryButton).toBeTruthy()
    await retryButton?.trigger('click')
    await flushPromises()

    expect(listRuns).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-test="async-content"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('河道巡检')
  })

  it('空列表显示真实空状态，不渲染表格行', async () => {
    vi.mocked(listRuns).mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      size: 20,
    })

    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('[data-test="async-empty"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('暂无运行记录')
    expect(wrapper.findAll('.el-table__row')).toHaveLength(0)
  })
})

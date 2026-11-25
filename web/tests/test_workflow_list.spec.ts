import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'
import { nextTick } from 'vue'

import type { UserRole } from '../src/api/http'
import type { WorkflowDetail, WorkflowListItem } from '../src/api/workflows'

// 认证仓库只依赖 role，保持 mock 轻量且不依赖 @pinia/testing。
const authState = vi.hoisted(() => ({ role: 'operator' as UserRole }))
const routerPush = vi.hoisted(() => vi.fn())

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => authState,
}))

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: routerPush }),
}))

vi.mock('@/api/workflows', () => ({
  listWorkflows: vi.fn(),
  createWorkflow: vi.fn(),
}))

import { ApiError } from '../src/api/http'
import { createWorkflow, listWorkflows } from '../src/api/workflows'
import Workflows from '../src/views/Workflows.vue'

function makeItem(overrides: Partial<WorkflowListItem> = {}): WorkflowListItem {
  return {
    id: 1,
    name: '巡检工作流',
    description: null,
    status: 'published',
    current_version: 3,
    created_at: '2026-11-20T10:00:00',
    updated_at: '2026-11-21T12:30:00',
    last_run: null,
    ...overrides,
  }
}

function makeDetail(overrides: Partial<WorkflowDetail> = {}): WorkflowDetail {
  return {
    id: 42,
    name: '新工作流',
    description: null,
    status: 'draft',
    current_version: 0,
    draft_version: 0,
    dsl: { nodes: [], edges: [] },
    created_by: 1,
    created_at: '2026-11-22T09:00:00',
    updated_at: '2026-11-22T09:00:00',
    last_run: null,
    ...overrides,
  }
}

function mountPage() {
  return mount(Workflows, {
    global: {
      plugins: [ElementPlus],
    },
  })
}

function findButton(wrapper: ReturnType<typeof mountPage>, text: string) {
  return wrapper.findAll('button').find((button) => button.text().trim() === text)
}

beforeEach(() => {
  vi.clearAllMocks()
  authState.role = 'operator'
})

describe('工作流列表页', () => {
  it('渲染接口返回的名称、状态、版本与最近运行', async () => {
    vi.mocked(listWorkflows).mockResolvedValue({
      items: [
        makeItem({
          id: 1,
          name: '巡检工作流',
          status: 'published',
          current_version: 3,
          last_run: {
            id: 9,
            status: 'succeeded',
            created_at: '2026-11-21T12:00:00',
          },
        }),
        makeItem({
          id: 2,
          name: '告警工作流',
          status: 'draft',
          current_version: 0,
          updated_at: null,
          last_run: null,
        }),
      ],
      total: 2,
      page: 1,
      size: 20,
    })

    const wrapper = mountPage()
    await flushPromises()

    expect(listWorkflows).toHaveBeenCalledWith({ page: 1, size: 20 })
    expect(wrapper.find('[data-test="async-content"]').exists()).toBe(true)

    const text = wrapper.text()
    expect(text).toContain('巡检工作流')
    expect(text).toContain('告警工作流')
    expect(text).toContain('已发布')
    expect(text).toContain('草稿')
    expect(text).toContain('v3')
    expect(text).toContain('未发布')
    expect(text).toContain('成功')
    expect(text).toContain('2026-11-21 12:00')
    expect(text).toContain('暂无运行')
    expect(text).toContain('—')
  })

  it('空列表只显示真实的空状态，不渲染占位行', async () => {
    vi.mocked(listWorkflows).mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      size: 20,
    })

    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('[data-test="async-empty"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('暂无工作流')
    expect(wrapper.find('[data-test="create-workflow-empty"]').exists()).toBe(true)
    expect(wrapper.find('.el-table').exists()).toBe(false)
    expect(wrapper.findAll('.el-table__row')).toHaveLength(0)
  })

  it('加载失败显示后端错误，重试会再次请求并渲染结果', async () => {
    vi.mocked(listWorkflows)
      .mockRejectedValueOnce(new ApiError(500, 500, '工作流列表加载失败'))
      .mockResolvedValueOnce({
        items: [makeItem()],
        total: 1,
        page: 1,
        size: 20,
      })

    const wrapper = mountPage()
    await flushPromises()

    expect(wrapper.find('[data-test="async-error"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('工作流列表加载失败')

    const retryButton = findButton(wrapper, '重试')
    expect(retryButton).toBeTruthy()
    await retryButton?.trigger('click')
    await flushPromises()

    expect(listWorkflows).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[data-test="async-content"]').exists()).toBe(true)
    expect(wrapper.text()).toContain('巡检工作流')
  })

  it('查看者看不到新建入口，操作员可创建并跳转编辑器', async () => {
    vi.mocked(listWorkflows).mockResolvedValue({
      items: [],
      total: 0,
      page: 1,
      size: 20,
    })

    authState.role = 'viewer'
    const viewerWrapper = mountPage()
    await flushPromises()

    expect(viewerWrapper.find('[data-test="create-workflow"]').exists()).toBe(false)
    expect(viewerWrapper.find('[data-test="create-workflow-empty"]').exists()).toBe(false)

    authState.role = 'operator'
    vi.mocked(createWorkflow).mockResolvedValue(makeDetail())

    const operatorWrapper = mountPage()
    await flushPromises()

    const createButton = operatorWrapper.find('[data-test="create-workflow"]')
    expect(createButton.exists()).toBe(true)

    await createButton.trigger('click')
    await nextTick()

    const nameInput = operatorWrapper.find('input[placeholder="请输入工作流名称"]')
    expect(nameInput.exists()).toBe(true)
    await nameInput.setValue('  新工作流  ')

    const submitButton = operatorWrapper.find('[data-test="submit-create"]')
    expect(submitButton.exists()).toBe(true)
    await submitButton.trigger('click')
    await flushPromises()

    expect(createWorkflow).toHaveBeenCalledWith({
      name: '新工作流',
      description: null,
      dsl: { nodes: [], edges: [] },
    })
    expect(routerPush).toHaveBeenCalledWith({
      name: 'workflow-editor',
      params: { id: 42 },
    })
  })

  it('点击“编辑”按 id 跳转编辑器路由', async () => {
    vi.mocked(listWorkflows).mockResolvedValue({
      items: [makeItem({ id: 7 })],
      total: 1,
      page: 1,
      size: 20,
    })

    const wrapper = mountPage()
    await flushPromises()

    const editButton = findButton(wrapper, '编辑')
    expect(editButton).toBeTruthy()
    await editButton?.trigger('click')

    expect(routerPush).toHaveBeenCalledWith({
      name: 'workflow-editor',
      params: { id: 7 },
    })
  })

  it('创建失败时展示后端错误信息且不跳转', async () => {
    vi.mocked(listWorkflows).mockResolvedValue({
      items: [makeItem()],
      total: 1,
      page: 1,
      size: 20,
    })
    vi.mocked(createWorkflow).mockRejectedValue(
      new ApiError(409, 409, '工作流名称已存在'),
    )

    const wrapper = mountPage()
    await flushPromises()

    await wrapper.find('[data-test="create-workflow"]').trigger('click')
    await nextTick()
    await wrapper.find('input[placeholder="请输入工作流名称"]').setValue('重复名称')
    await wrapper.find('[data-test="submit-create"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('工作流名称已存在')
    expect(routerPush).not.toHaveBeenCalled()
  })

  it('翻页时按新页码重新请求', async () => {
    vi.mocked(listWorkflows)
      .mockResolvedValueOnce({
        items: [makeItem()],
        total: 45,
        page: 1,
        size: 20,
      })
      .mockResolvedValueOnce({
        items: [makeItem({ id: 21, name: '第二页工作流' })],
        total: 45,
        page: 2,
        size: 20,
      })

    const wrapper = mountPage()
    await flushPromises()

    const page2 = wrapper.findAll('.el-pager li').find((item) => item.text() === '2')
    expect(page2).toBeTruthy()
    await page2?.trigger('click')
    await flushPromises()

    expect(listWorkflows).toHaveBeenLastCalledWith({ page: 2, size: 20 })
    expect(wrapper.text()).toContain('第二页工作流')
  })
})

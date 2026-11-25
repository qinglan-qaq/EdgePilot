import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import type { UserRole } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

import AppLayout from '@/layouts/AppLayout.vue'
import Forbidden from '@/views/Forbidden.vue'
import Login from '@/views/Login.vue'
import ModulePending from '@/views/ModulePending.vue'
import NotFound from '@/views/NotFound.vue'
import Workflows from '@/views/Workflows.vue'

import { canAccessRoles } from './access'
import { sanitizeRedirect } from './redirect'

declare module 'vue-router' {
  interface RouteMeta {
    /** 浏览器标题与页面主标题 */
    title?: string
    /** 是否出现在侧边导航 */
    nav?: boolean
    /** 侧边导航图标标识，由 AppLayout 映射到具体组件 */
    icon?: string
    /** 允许访问的角色；不声明表示所有已登录角色均可访问 */
    roles?: UserRole[]
    /** 公共路由，无需登录 */
    public?: boolean
    /** 占位页说明文案 */
    description?: string
    /** 详情页高亮的侧边导航路径 */
    activeMenu?: string
  }
}

const ALL_ROLES: UserRole[] = ['viewer', 'operator', 'admin']

const routes: RouteRecordRaw[] = [
  {
    path: '/login',
    name: 'login',
    component: Login,
    meta: {
      title: '登录',
      public: true,
    },
  },
  {
    path: '/',
    component: AppLayout,
    children: [
      {
        path: '',
        name: 'dashboard',
        component: ModulePending,
        meta: {
          title: '概览',
          nav: true,
          icon: 'dashboard',
          roles: ALL_ROLES,
          description: '概览页将在后续任务中接入真实的节点、设备、事件与运行统计。',
        },
      },
      {
        path: 'agents',
        name: 'agents',
        component: ModulePending,
        meta: {
          title: 'Agent 管理',
          nav: true,
          icon: 'agents',
          roles: ALL_ROLES,
          description: 'Agent 列表、提示词编辑、工具绑定与版本发布将在后续任务中接入。',
        },
      },
      {
        path: 'workflows',
        name: 'workflows',
        component: Workflows,
        meta: {
          title: '工作流',
          nav: true,
          icon: 'workflows',
          roles: ALL_ROLES,
        },
      },
      {
        path: 'workflows/:id',
        name: 'workflow-editor',
        component: ModulePending,
        meta: {
          title: '工作流编辑器',
          roles: ALL_ROLES,
          activeMenu: '/workflows',
          description: 'Vue Flow 编辑器将在此接入真实草稿与发布流程。',
        },
      },
      {
        path: 'runs',
        name: 'runs',
        component: ModulePending,
        meta: {
          title: '运行追踪',
          nav: true,
          icon: 'runs',
          roles: ALL_ROLES,
          description: '运行记录、节点状态与实时追踪将在后续任务中接入。',
        },
      },
      {
        path: 'nodes',
        name: 'nodes',
        component: ModulePending,
        meta: {
          title: '节点',
          nav: true,
          icon: 'nodes',
          roles: ALL_ROLES,
          description: '边缘节点心跳、在线状态与指标将在后续任务中接入。',
        },
      },
      {
        path: 'devices',
        name: 'devices',
        component: ModulePending,
        meta: {
          title: '设备',
          nav: true,
          icon: 'devices',
          roles: ALL_ROLES,
          description: '相机设备、RTSP 探测与抓帧预览将在后续任务中接入。',
        },
      },
      {
        path: 'events',
        name: 'events',
        component: ModulePending,
        meta: {
          title: '事件中心',
          nav: true,
          icon: 'events',
          roles: ALL_ROLES,
          description: '实时事件流、缩略图与事件详情将在后续任务中接入。',
        },
      },
      {
        path: 'knowledge',
        name: 'knowledge',
        component: ModulePending,
        meta: {
          title: '知识库',
          nav: true,
          icon: 'knowledge',
          roles: ALL_ROLES,
          description: '文档上传、列表与检索调试将在后续任务中接入。',
        },
      },
      {
        path: 'settings',
        name: 'settings',
        component: ModulePending,
        meta: {
          title: '系统设置',
          nav: true,
          icon: 'settings',
          roles: ['admin'],
          description: '用户管理、审计日志与降级模式设置将在后续任务中接入。',
        },
      },
    ],
  },
  {
    path: '/403',
    name: 'forbidden',
    component: Forbidden,
    meta: {
      title: '无权访问',
    },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: NotFound,
    meta: {
      title: '页面不存在',
    },
  },
]

const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()

  // 首次导航时完成登录态恢复，之后调用会立即返回
  await auth.bootstrap()

  if (to.meta.public) {
    if (to.path === '/login' && auth.isAuthenticated) {
      return '/'
    }
    return true
  }

  if (!auth.isAuthenticated) {
    const redirect = sanitizeRedirect(to.fullPath)
    return {
      name: 'login',
      query: redirect && redirect !== '/' ? { redirect } : {},
    }
  }

  if (!canAccessRoles(to.meta.roles, auth.role)) {
    return { name: 'forbidden' }
  }

  return true
})

router.afterEach((to) => {
  if (typeof document !== 'undefined') {
    document.title = to.meta.title ? `${to.meta.title} · EdgePilot` : 'EdgePilot'
  }
})

export default router

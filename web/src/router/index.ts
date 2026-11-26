import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

import type { UserRole } from '@/api/http'
import { useAuthStore } from '@/stores/auth'

import AppLayout from '@/layouts/AppLayout.vue'
import Forbidden from '@/views/Forbidden.vue'
import Login from '@/views/Login.vue'
import ModulePending from '@/views/ModulePending.vue'
import NotFound from '@/views/NotFound.vue'
import ObservabilityLayout from '@/views/observability/ObservabilityLayout.vue'
import Agents from '@/views/observability/Agents.vue'
import Models from '@/views/observability/Models.vue'
import Overview from '@/views/observability/Overview.vue'
import Playground from '@/views/observability/Playground.vue'
import Prompts from '@/views/observability/Prompts.vue'
import Settings from '@/views/observability/Settings.vue'
import Tools from '@/views/observability/Tools.vue'
import TraceDetail from '@/views/observability/TraceDetail.vue'
import Traces from '@/views/observability/Traces.vue'
import RunDetail from '@/views/RunDetail.vue'
import Runs from '@/views/Runs.vue'
import WorkflowEditor from '@/views/WorkflowEditor.vue'
import Workflows from '@/views/Workflows.vue'
import VisionDetect from '@/views/vision/Detect.vue'
import VisionModels from '@/views/vision/Models.vue'
import VisionOverview from '@/views/vision/Overview.vue'
import VisionRecords from '@/views/vision/Records.vue'
import VisionTaskDetail from '@/views/vision/TaskDetail.vue'
import VisionLayout from '@/views/vision/VisionLayout.vue'

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
        component: WorkflowEditor,
        meta: {
          title: '工作流编辑器',
          roles: ALL_ROLES,
          activeMenu: '/workflows',
        },
      },
      {
        path: 'runs',
        name: 'runs',
        component: Runs,
        meta: {
          title: '运行追踪',
          nav: true,
          icon: 'runs',
          roles: ALL_ROLES,
        },
      },
      {
        path: 'runs/:id',
        name: 'run-detail',
        component: RunDetail,
        meta: {
          title: '运行详情',
          roles: ALL_ROLES,
          activeMenu: '/runs',
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
        path: 'observability',
        name: 'observability',
        component: ObservabilityLayout,
        meta: {
          title: 'AI Observability',
          nav: true,
          icon: 'observability',
          roles: ALL_ROLES,
        },
        children: [
          {
            path: '',
            name: 'observability-overview',
            component: Overview,
            meta: {
              title: '可观测概览',
              nav: true,
              roles: ALL_ROLES,
              activeMenu: '/observability',
            },
          },
          {
            path: 'traces',
            name: 'observability-traces',
            component: Traces,
            meta: {
              title: 'Trace 查询',
              nav: true,
              roles: ALL_ROLES,
              activeMenu: '/observability/traces',
            },
          },
          {
            path: 'traces/:traceId',
            name: 'observability-trace-detail',
            component: TraceDetail,
            meta: {
              title: 'Trace 详情',
              nav: false,
              roles: ALL_ROLES,
              activeMenu: '/observability/traces',
            },
          },
          {
            path: 'tools',
            name: 'observability-tools',
            component: Tools,
            meta: {
              title: '工具分析',
              nav: true,
              roles: ALL_ROLES,
              activeMenu: '/observability/tools',
            },
          },
          {
            path: 'models',
            name: 'observability-models',
            component: Models,
            meta: {
              title: '模型分析',
              nav: true,
              roles: ALL_ROLES,
              activeMenu: '/observability/models',
            },
          },
          {
            path: 'agents',
            name: 'observability-agents',
            component: Agents,
            meta: {
              title: 'Agent 分析',
              nav: true,
              roles: ALL_ROLES,
              activeMenu: '/observability/agents',
            },
          },
          {
            // 精确静态路径，放在 prompts 路由之前，避免被更宽的前缀影响。
            path: 'prompts/playground',
            name: 'observability-prompt-playground',
            component: Playground,
            meta: {
              title: 'Prompt Playground',
              nav: false,
              roles: ALL_ROLES,
              activeMenu: '/observability/prompts',
            },
          },
          {
            path: 'prompts',
            name: 'observability-prompts',
            component: Prompts,
            meta: {
              title: '提示词',
              nav: true,
              roles: ALL_ROLES,
              activeMenu: '/observability/prompts',
            },
          },
          {
            path: 'settings',
            name: 'observability-settings',
            component: Settings,
            meta: {
              title: '可观测设置',
              nav: true,
              roles: ['admin'],
              activeMenu: '/observability/settings',
            },
          },
        ],
      },
      {
        path: 'vision',
        name: 'vision',
        component: VisionLayout,
        meta: {
          title: '视频识别',
          nav: true,
          icon: 'vision',
          roles: ALL_ROLES,
        },
        children: [
          {
            path: '',
            name: 'vision-overview',
            component: VisionOverview,
            meta: {
              title: '识别概览',
              nav: true,
              roles: ALL_ROLES,
              activeMenu: '/vision',
            },
          },
          {
            path: 'detect',
            name: 'vision-detect',
            component: VisionDetect,
            meta: {
              title: '视频识别',
              nav: true,
              roles: ALL_ROLES,
              activeMenu: '/vision/detect',
            },
          },
          {
            path: 'records',
            name: 'vision-records',
            component: VisionRecords,
            meta: {
              title: '识别记录',
              nav: true,
              roles: ALL_ROLES,
              activeMenu: '/vision/records',
            },
          },
          {
            path: 'records/:taskUid',
            name: 'vision-task-detail',
            component: VisionTaskDetail,
            meta: {
              title: '识别详情',
              nav: false,
              roles: ALL_ROLES,
              activeMenu: '/vision/records',
            },
          },
          {
            path: 'models',
            name: 'vision-models',
            component: VisionModels,
            meta: {
              title: '模型与许可',
              nav: true,
              roles: ALL_ROLES,
              activeMenu: '/vision/models',
            },
          },
        ],
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

/**
 * 全局路由守卫：恢复登录态 → 公共路由放行 → 未登录跳登录 → 角色校验。
 *
 * 关键节点：
 * 1. 每次导航先等待 `auth.bootstrap()`；内部共享 Promise，只真正执行一次；
 * 2. 已登录用户访问 /login 直接回首页，避免重复登录；
 * 3. 未登录时把原始路径放进 redirect，登录后可回到用户想去的页面；
 * 4. 角色不足统一跳 /403，不向后端发无权限请求。
 */
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

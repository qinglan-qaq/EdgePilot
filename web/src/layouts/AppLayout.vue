<script setup lang="ts">
import { computed, ref, type Component } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import {
  Avatar,
  Bell,
  Camera,
  Connection,
  DataLine,
  Odometer,
  Reading,
  Setting,
  Share,
  VideoPlay,
  View,
} from '@element-plus/icons-vue'

import type { UserRole } from '@/api/http'
import { canAccessRoles } from '@/router/access'
import { useAuthStore } from '@/stores/auth'

interface NavChild {
  path: string
  title: string
}

interface NavItem {
  path: string
  title: string
  icon?: Component
  children?: NavChild[]
}

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const loggingOut = ref(false)

const iconMap: Record<string, Component> = {
  dashboard: Odometer,
  agents: Avatar,
  workflows: Share,
  runs: VideoPlay,
  observability: DataLine,
  vision: View,
  nodes: Connection,
  devices: Camera,
  events: Bell,
  knowledge: Reading,
  settings: Setting,
}

const roleLabels: Record<UserRole, string> = {
  viewer: '查看者',
  operator: '操作员',
  admin: '管理员',
}

const roleTagTypes: Record<UserRole, 'info' | 'warning' | 'danger'> = {
  viewer: 'info',
  operator: 'warning',
  admin: 'danger',
}

const navItems = computed<NavItem[]>(() => {
  const layoutRoute = router.options.routes.find((item) => item.path === '/')
  const items: NavItem[] = []

  for (const child of layoutRoute?.children ?? []) {
    if (!child.meta?.nav || !child.name) {
      continue
    }
    if (!canAccessRoles(child.meta.roles, auth.role)) {
      continue
    }

    const children: NavChild[] = []
    for (const nested of child.children ?? []) {
      if (!nested.meta?.nav || !nested.name) {
        continue
      }
      if (!canAccessRoles(nested.meta.roles, auth.role)) {
        continue
      }
      children.push({
        path: router.resolve({ name: nested.name }).path,
        title: nested.meta.title ?? '未命名页面',
      })
    }

    items.push({
      path: router.resolve({ name: child.name }).path,
      title: child.meta.title ?? '未命名模块',
      icon: child.meta.icon ? iconMap[child.meta.icon] : undefined,
      ...(children.length > 0 ? { children } : {}),
    })
  }

  return items
})

const activeMenu = computed(() => route.meta.activeMenu ?? route.path)
const pageTitle = computed(() => route.meta.title ?? 'EdgePilot')
const displayName = computed(
  () => auth.user?.display_name?.trim() || auth.user?.email || '未登录用户',
)
const roleLabel = computed(() => (auth.role ? roleLabels[auth.role] : '未知角色'))
const roleTagType = computed(() => (auth.role ? roleTagTypes[auth.role] : 'info'))

/**
 * 退出登录：清本地状态后回到登录页。
 *
 * 关键节点：`auth.logout()` 内部已保证后端失败也清理本地令牌；
 * loggingOut 防止重复点击触发多次跳转。
 */
async function handleLogout(): Promise<void> {
  if (loggingOut.value) {
    return
  }
  loggingOut.value = true
  try {
    await auth.logout()
    ElMessage.success('已退出登录')
    await router.replace({ name: 'login' })
  } finally {
    loggingOut.value = false
  }
}
</script>

<template>
  <el-container class="app-shell">
    <el-aside class="app-aside" width="224px">
      <div class="app-brand">
        <span class="app-brand__mark">EP</span>
        <div>
          <p class="app-brand__name">EdgePilot</p>
          <p class="app-brand__desc">边缘智能运维</p>
        </div>
      </div>

      <el-menu :default-active="activeMenu" router class="app-menu">
        <template v-for="item in navItems" :key="item.path">
          <el-sub-menu v-if="item.children?.length" :index="item.path">
            <template #title>
              <el-icon v-if="item.icon"><component :is="item.icon" /></el-icon>
              <span>{{ item.title }}</span>
            </template>
            <el-menu-item
              v-for="child in item.children"
              :key="child.path"
              :index="child.path"
            >
              <span>{{ child.title }}</span>
            </el-menu-item>
          </el-sub-menu>

          <el-menu-item v-else :index="item.path">
            <el-icon v-if="item.icon"><component :is="item.icon" /></el-icon>
            <span>{{ item.title }}</span>
          </el-menu-item>
        </template>
      </el-menu>

      <div class="app-aside__footer">控制台</div>
    </el-aside>

    <el-container class="app-body">
      <el-header class="app-header" height="64px">
        <div class="app-header__title">
          <span class="app-header__kicker">EdgePilot</span>
          <h1>{{ pageTitle }}</h1>
        </div>

        <div class="app-header__user">
          <el-tag :type="roleTagType" effect="light" size="small">{{ roleLabel }}</el-tag>
          <span class="app-header__name">{{ displayName }}</span>
          <el-divider direction="vertical" />
          <el-button text :loading="loggingOut" @click="handleLogout">退出登录</el-button>
        </div>
      </el-header>

      <el-main class="app-main">
        <router-view />
      </el-main>
    </el-container>
  </el-container>
</template>

<style scoped>
.app-shell {
  height: 100%;
  background: #f3f5f9;
}

.app-aside {
  display: flex;
  flex-direction: column;
  background: linear-gradient(180deg, #0b1526 0%, #0f1d33 100%);
  color: #e2e8f0;
}

.app-brand {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 20px 18px;
  border-bottom: 1px solid rgba(148, 163, 184, 0.16);
}

.app-brand__mark {
  display: inline-flex;
  width: 36px;
  height: 36px;
  align-items: center;
  justify-content: center;
  border-radius: 10px;
  background: linear-gradient(135deg, #3b82f6, #06b6d4);
  color: #ffffff;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.08em;
}

.app-brand__name {
  margin: 0;
  font-size: 15px;
  font-weight: 700;
  color: #f8fafc;
}

.app-brand__desc {
  margin: 2px 0 0;
  font-size: 11px;
  color: rgba(226, 232, 240, 0.6);
}

.app-menu {
  flex: 1;
  padding: 10px 8px;
  border-right: none;
  background: transparent;
}

.app-menu :deep(.el-menu-item) {
  height: 44px;
  margin-bottom: 4px;
  border-radius: 8px;
  color: rgba(226, 232, 240, 0.76);
}

.app-menu :deep(.el-menu-item:hover) {
  background: rgba(59, 130, 246, 0.14);
  color: #f8fafc;
}

.app-menu :deep(.el-menu-item.is-active) {
  background: rgba(59, 130, 246, 0.24);
  color: #ffffff;
  font-weight: 600;
}

.app-aside__footer {
  padding: 14px 18px;
  border-top: 1px solid rgba(148, 163, 184, 0.14);
  color: rgba(226, 232, 240, 0.42);
  font-size: 11px;
  letter-spacing: 0.08em;
}

.app-body {
  min-width: 0;
}

.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 24px;
  border-bottom: 1px solid #e5e9f2;
  background: #ffffff;
}

.app-header__kicker {
  display: block;
  color: #93a1b5;
  font-size: 11px;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}

.app-header__title h1 {
  margin: 2px 0 0;
  font-size: 18px;
  font-weight: 600;
  color: #111827;
}

.app-header__user {
  display: flex;
  align-items: center;
  gap: 10px;
  color: #334155;
}

.app-header__name {
  max-width: 220px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  color: #475569;
}

.app-main {
  padding: 24px;
  overflow: auto;
}
</style>

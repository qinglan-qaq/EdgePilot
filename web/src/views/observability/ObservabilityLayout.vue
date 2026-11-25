<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import { canAccessRoles } from '@/router/access'
import { useAuthStore } from '@/stores/auth'

interface ObservabilityTab {
  path: string
  label: string
  roles?: ['admin']
}

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const ALL_TABS: ObservabilityTab[] = [
  { path: '/observability', label: '概览' },
  { path: '/observability/traces', label: 'Trace 查询' },
  { path: '/observability/tools', label: '工具分析' },
  { path: '/observability/models', label: '模型分析' },
  { path: '/observability/agents', label: 'Agent 分析' },
  { path: '/observability/prompts', label: '提示词' },
  { path: '/observability/settings', label: '设置', roles: ['admin'] },
]

const tabs = computed(() =>
  ALL_TABS.filter((tab) => canAccessRoles(tab.roles, auth.role)),
)

/** 用最长路径前缀匹配，保证 /observability/traces/xxx 仍高亮 Trace 查询。 */
const activeTab = computed(() => {
  const matched = tabs.value
    .filter(
      (tab) => route.path === tab.path || route.path.startsWith(`${tab.path}/`),
    )
    .sort((left, right) => right.path.length - left.path.length)
  return matched[0]?.path ?? '/observability'
})

function handleTabChange(name: string | number): void {
  const target = String(name)
  if (target !== route.path) {
    void router.push(target)
  }
}
</script>

<template>
  <section class="observability-layout">
    <el-tabs
      :model-value="activeTab"
      class="observability-layout__tabs"
      data-test="observability-tabs"
      @tab-change="handleTabChange"
    >
      <el-tab-pane
        v-for="tab in tabs"
        :key="tab.path"
        :label="tab.label"
        :name="tab.path"
      />
    </el-tabs>

    <router-view />
  </section>
</template>

<style scoped>
.observability-layout {
  width: 100%;
}

.observability-layout__tabs {
  margin-bottom: 16px;
  padding: 0 16px;
  border-radius: 10px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.observability-layout__tabs :deep(.el-tabs__header) {
  margin: 0;
}

.observability-layout__tabs :deep(.el-tabs__nav-wrap::after) {
  display: none;
}

.observability-layout__tabs :deep(.el-tabs__item) {
  height: 48px;
  font-size: 13px;
}

.observability-layout__tabs :deep(.el-tabs__content) {
  display: none;
}
</style>

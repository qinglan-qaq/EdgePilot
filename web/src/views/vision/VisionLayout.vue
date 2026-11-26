<script setup lang="ts">
import { computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'

const route = useRoute()
const router = useRouter()

interface VisionTab {
  path: string
  label: string
}

const TABS: VisionTab[] = [
  { path: '/vision', label: '识别概览' },
  { path: '/vision/detect', label: '视频识别' },
  { path: '/vision/records', label: '识别记录' },
  { path: '/vision/models', label: '模型与许可' },
]

/** 最长路径前缀匹配：详情页 /vision/records/xxx 仍高亮识别记录。 */
const activeTab = computed(() => {
  const matched = TABS.filter(
    (tab) => route.path === tab.path || route.path.startsWith(`${tab.path}/`),
  ).sort((left, right) => right.path.length - left.path.length)
  return matched[0]?.path ?? '/vision'
})

function handleTabChange(name: string | number): void {
  const target = String(name)
  if (target !== route.path) {
    void router.push(target)
  }
}
</script>

<template>
  <section class="vision-layout">
    <el-tabs
      :model-value="activeTab"
      class="vision-layout__tabs"
      data-test="vision-tabs"
      @tab-change="handleTabChange"
    >
      <el-tab-pane
        v-for="tab in TABS"
        :key="tab.path"
        :label="tab.label"
        :name="tab.path"
      />
    </el-tabs>

    <router-view />
  </section>
</template>

<style scoped>
.vision-layout {
  width: 100%;
}

.vision-layout__tabs {
  margin-bottom: 16px;
  padding: 0 16px;
  border-radius: 10px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.vision-layout__tabs :deep(.el-tabs__header) {
  margin: 0;
}

.vision-layout__tabs :deep(.el-tabs__nav-wrap::after) {
  display: none;
}

.vision-layout__tabs :deep(.el-tabs__item) {
  height: 48px;
  font-size: 13px;
}

.vision-layout__tabs :deep(.el-tabs__content) {
  display: none;
}
</style>

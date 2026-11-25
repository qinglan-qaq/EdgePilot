<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'

const route = useRoute()

const title = computed(() => route.meta.title ?? '该模块')
const description = computed(
  () => route.meta.description ?? '当前页面的数据接口尚未接入，暂不展示任何内容。',
)
</script>

<template>
  <section class="module-pending">
    <el-card shadow="never" class="module-pending__card">
      <header class="module-pending__header">
        <h2>{{ title }}</h2>
        <el-tag type="info" effect="plain">待接入</el-tag>
      </header>

      <el-empty description="模块待接入">
        <p class="module-pending__description">{{ description }}</p>
        <p class="module-pending__hint">
          后续任务将接入真实接口与数据；本页不展示模拟统计、模拟列表或占位图表。
        </p>
      </el-empty>
    </el-card>
  </section>
</template>

<style scoped>
.module-pending {
  max-width: 960px;
}

.module-pending__card {
  border-radius: 12px;
}

.module-pending__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 8px;
  border-bottom: 1px solid #eef1f6;
}

.module-pending__header h2 {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #111827;
}

.module-pending__description {
  margin: 0 0 8px;
  color: #4b5563;
  line-height: 1.7;
}

.module-pending__hint {
  margin: 0;
  color: #9ca3af;
  font-size: 13px;
  line-height: 1.7;
}
</style>

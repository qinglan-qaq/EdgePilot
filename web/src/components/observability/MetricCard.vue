<script setup lang="ts">
import { computed } from 'vue'
import { InfoFilled } from '@element-plus/icons-vue'

const props = withDefaults(
  defineProps<{
    /** 指标唯一标识，用于测试与样式定位 */
    metric: string
    label: string
    /** 已格式化好的展示值（null 应在调用方转成“未上报”） */
    value: string
    /** 中文口径说明，随 tooltip 展示 */
    hint: string
    /** 附加说明行 */
    detail?: string | null
    /** 0..1 的进度，仅在有明确分母时传入 */
    progress?: number | null
  }>(),
  {
    detail: null,
    progress: null,
  },
)

const progressPct = computed(() => {
  if (props.progress === null || props.progress === undefined) {
    return null
  }
  const clamped = Math.min(1, Math.max(0, props.progress))
  return `${(clamped * 100).toFixed(0)}%`
})
</script>

<template>
  <el-card
    shadow="never"
    class="metric-card"
    data-test="metric-card"
    :data-metric="metric"
  >
    <div class="metric-card__header">
      <span class="metric-card__label">{{ label }}</span>
      <el-tooltip :content="hint" placement="top" effect="dark">
        <span
          class="metric-card__hint"
          data-test="metric-hint"
          :data-hint="hint"
          tabindex="0"
          aria-label="指标口径说明"
        >
          <el-icon><InfoFilled /></el-icon>
        </span>
      </el-tooltip>
    </div>

    <p class="metric-card__value">{{ value }}</p>

    <p v-if="detail" class="metric-card__detail">{{ detail }}</p>

    <div
      v-if="progressPct !== null"
      class="metric-card__progress"
      role="progressbar"
      :aria-valuemin="0"
      :aria-valuemax="1"
      :aria-valuenow="props.progress ?? 0"
    >
      <span class="metric-card__progress-bar" :style="{ width: progressPct }" />
    </div>
  </el-card>
</template>

<style scoped>
.metric-card {
  height: 100%;
  border-radius: 10px;
}

.metric-card :deep(.el-card__body) {
  display: flex;
  height: 100%;
  flex-direction: column;
  gap: 6px;
  padding: 16px;
}

.metric-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.metric-card__label {
  color: #64748b;
  font-size: 13px;
}

.metric-card__hint {
  display: inline-flex;
  color: #94a3b8;
  cursor: help;
  outline: none;
}

.metric-card__hint:hover,
.metric-card__hint:focus-visible {
  color: #2563eb;
}

.metric-card__value {
  margin: 0;
  color: #0f172a;
  font-size: 22px;
  font-weight: 600;
  line-height: 1.2;
  word-break: break-all;
}

.metric-card__detail {
  margin: 0;
  color: #94a3b8;
  font-size: 12px;
  line-height: 1.5;
}

.metric-card__progress {
  height: 6px;
  overflow: hidden;
  border-radius: 999px;
  background: #eef2f7;
}

.metric-card__progress-bar {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, #3b82f6, #06b6d4);
}
</style>

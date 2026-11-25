<script setup lang="ts">
import { computed } from 'vue'

import type { BarListItem } from './chart-types'

const props = withDefaults(
  defineProps<{
    items: BarListItem[]
    /** 数值格式化；null 固定显示“未上报” */
    valueFormatter?: (value: number | null) => string
    /** 进度条分母；成功率等有固定上限的指标应传 1，而不是按最大值归一化 */
    max?: number | null
    emptyText?: string
  }>(),
  {
    valueFormatter: undefined,
    max: null,
    emptyText: '暂无数据',
  },
)

const formatter = computed<(value: number | null) => string>(() => {
  return props.valueFormatter ?? ((value: number | null) => String(value))
})

const maxValue = computed(() => {
  if (typeof props.max === 'number' && Number.isFinite(props.max) && props.max > 0) {
    return props.max
  }
  let max = 0
  for (const item of props.items) {
    if (typeof item.value === 'number' && Number.isFinite(item.value)) {
      max = Math.max(max, item.value)
    }
  }
  return max
})

const hasItems = computed(() => props.items.length > 0)

function displayValue(value: number | null): string {
  return value === null ? '未上报' : formatter.value(value)
}

function barWidth(value: number | null): string {
  if (value === null || maxValue.value <= 0) {
    return '0%'
  }
  const ratio = Math.min(1, Math.max(0, value / maxValue.value))
  return `${(ratio * 100).toFixed(1)}%`
}
</script>

<template>
  <div class="bar-list" data-test="bar-list">
    <p v-if="!hasItems" class="bar-list__empty">{{ emptyText }}</p>

    <ul v-else class="bar-list__items">
      <li
        v-for="(item, index) in items"
        :key="`${item.label}-${index}`"
        class="bar-list__item"
        data-test="bar-list-item"
      >
        <div class="bar-list__head">
          <span class="bar-list__label" :title="item.label">{{ item.label }}</span>
          <span class="bar-list__value">{{ displayValue(item.value) }}</span>
        </div>

        <div class="bar-list__track" aria-hidden="true">
          <span
            v-if="item.value !== null"
            class="bar-list__bar"
            :class="item.tone ? `bar-list__bar--${item.tone}` : undefined"
            :style="{ width: barWidth(item.value) }"
          />
        </div>

        <p v-if="item.detail" class="bar-list__detail">{{ item.detail }}</p>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.bar-list {
  width: 100%;
}

.bar-list__empty {
  margin: 0;
  padding: 32px 0;
  color: #94a3b8;
  font-size: 13px;
  text-align: center;
}

.bar-list__items {
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.bar-list__head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 6px;
}

.bar-list__label {
  overflow: hidden;
  color: #334155;
  font-size: 13px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.bar-list__value {
  flex-shrink: 0;
  color: #0f172a;
  font-size: 13px;
  font-weight: 600;
}

.bar-list__track {
  width: 100%;
  height: 8px;
  overflow: hidden;
  border-radius: 999px;
  background: #f1f5f9;
}

.bar-list__bar {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: #3b82f6;
  transition: width 0.2s ease;
}

.bar-list__bar--success {
  background: #22c55e;
}

.bar-list__bar--warning {
  background: #f59e0b;
}

.bar-list__bar--danger {
  background: #ef4444;
}

.bar-list__bar--info {
  background: #06b6d4;
}

.bar-list__detail {
  margin: 6px 0 0;
  color: #94a3b8;
  font-size: 12px;
  line-height: 1.5;
}
</style>

<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { runStatusLabel } from '@/utils/status'
import type { TraceFilterState } from '@/utils/traceFilters'

const props = withDefaults(
  defineProps<{
    modelValue: TraceFilterState
    loading?: boolean
  }>(),
  {
    loading: false,
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: TraceFilterState]
  apply: []
  reset: []
}>()

const STATUS_OPTIONS = [
  { value: '', label: '全部状态' },
  { value: 'running', label: runStatusLabel('running') },
  { value: 'succeeded', label: runStatusLabel('succeeded') },
  { value: 'failed', label: runStatusLabel('failed') },
  { value: 'timeout', label: runStatusLabel('timeout') },
  { value: 'cancelled', label: runStatusLabel('cancelled') },
]

// 受控组件：本地保留草稿，父组件（URL 解析结果）变化时整体覆盖；
// 输入时先同步草稿再 emit，确保连续输入不会丢字段。
const draft = ref<TraceFilterState>({ ...props.modelValue })

watch(
  () => props.modelValue,
  (value) => {
    draft.value = { ...value }
  },
  { deep: true },
)

function commit(next: TraceFilterState): void {
  draft.value = next
  emit('update:modelValue', next)
}

function updateField<K extends keyof TraceFilterState>(
  key: K,
  value: TraceFilterState[K],
): void {
  if (draft.value[key] === value) {
    return
  }
  commit({ ...draft.value, [key]: value })
}

const dateRange = computed<[string, string] | null>(() => {
  const { startFrom, startTo } = draft.value
  if (!startFrom && !startTo) {
    return null
  }
  return [startFrom, startTo]
})

function rangeText(value: unknown): string {
  if (value instanceof Date) {
    return Number.isNaN(value.getTime()) ? '' : value.toISOString()
  }
  return typeof value === 'string' ? value : ''
}

function handleDateRange(value: unknown): void {
  if (!Array.isArray(value) || value.length < 2) {
    commit({ ...draft.value, startFrom: '', startTo: '' })
    return
  }
  commit({
    ...draft.value,
    startFrom: rangeText(value[0]),
    startTo: rangeText(value[1]),
  })
}

function handleApply(): void {
  emit('apply')
}

function handleReset(): void {
  emit('reset')
}
</script>

<template>
  <section class="filter-bar" data-test="trace-filter-bar">
    <div class="filter-bar__fields">
      <label class="filter-bar__field">
        <span class="filter-bar__label">状态</span>
        <el-select
          :model-value="draft.status"
          size="small"
          data-test="filter-status"
          @update:model-value="(value: string) => updateField('status', value)"
        >
          <el-option
            v-for="option in STATUS_OPTIONS"
            :key="option.value"
            :label="option.label"
            :value="option.value"
          />
        </el-select>
      </label>

      <label class="filter-bar__field">
        <span class="filter-bar__label">Trace ID 前缀</span>
        <el-input
          :model-value="draft.traceId"
          size="small"
          clearable
          placeholder="trace_id 前缀"
          data-test="filter-trace-id"
          @update:model-value="(value: string) => updateField('traceId', value)"
          @keyup.enter="handleApply"
        />
      </label>

      <label class="filter-bar__field">
        <span class="filter-bar__label">来源</span>
        <el-input
          :model-value="draft.source"
          size="small"
          clearable
          placeholder="如 workflow"
          data-test="filter-source"
          @update:model-value="(value: string) => updateField('source', value)"
          @keyup.enter="handleApply"
        />
      </label>

      <label class="filter-bar__field">
        <span class="filter-bar__label">用户 ID</span>
        <el-input
          :model-value="draft.userId"
          size="small"
          clearable
          placeholder="精确匹配"
          data-test="filter-user-id"
          @update:model-value="(value: string) => updateField('userId', value)"
          @keyup.enter="handleApply"
        />
      </label>

      <label class="filter-bar__field">
        <span class="filter-bar__label">Agent ID</span>
        <el-input
          :model-value="draft.agentId"
          size="small"
          clearable
          placeholder="整数"
          data-test="filter-agent-id"
          @update:model-value="(value: string) => updateField('agentId', value)"
          @keyup.enter="handleApply"
        />
      </label>

      <label class="filter-bar__field">
        <span class="filter-bar__label">模型</span>
        <el-input
          :model-value="draft.model"
          size="small"
          clearable
          placeholder="model_name 或 provider"
          data-test="filter-model"
          @update:model-value="(value: string) => updateField('model', value)"
          @keyup.enter="handleApply"
        />
      </label>

      <label class="filter-bar__field">
        <span class="filter-bar__label">工具</span>
        <el-input
          :model-value="draft.tool"
          size="small"
          clearable
          placeholder="tool_id 或 tool_name"
          data-test="filter-tool"
          @update:model-value="(value: string) => updateField('tool', value)"
          @keyup.enter="handleApply"
        />
      </label>

      <label class="filter-bar__field filter-bar__field--wide">
        <span class="filter-bar__label">开始时间范围</span>
        <el-date-picker
          :model-value="dateRange"
          type="datetimerange"
          size="small"
          value-format="YYYY-MM-DDTHH:mm:ss.SSSZ"
          range-separator="至"
          start-placeholder="开始时间"
          end-placeholder="结束时间"
          data-test="filter-time-range"
          @update:model-value="handleDateRange"
        />
      </label>

      <label class="filter-bar__field">
        <span class="filter-bar__label">最小耗时 (ms)</span>
        <el-input
          :model-value="draft.minDurationMs"
          size="small"
          clearable
          placeholder=">= 0"
          data-test="filter-min-duration"
          @update:model-value="(value: string) => updateField('minDurationMs', value)"
          @keyup.enter="handleApply"
        />
      </label>

      <div class="filter-bar__toggles">
        <label class="filter-bar__toggle">
          <el-switch
            :model-value="draft.failed"
            size="small"
            data-test="filter-failed"
            @update:model-value="
              (value: string | number | boolean) => updateField('failed', Boolean(value))
            "
          />
          <span>仅失败</span>
        </label>

        <label class="filter-bar__toggle">
          <el-switch
            :model-value="draft.slow"
            size="small"
            data-test="filter-slow"
            @update:model-value="
              (value: string | number | boolean) => updateField('slow', Boolean(value))
            "
          />
          <span>慢 Trace (&gt;=3 秒)</span>
        </label>
      </div>
    </div>

    <div class="filter-bar__actions">
      <el-button
        type="primary"
        size="small"
        :loading="loading"
        data-test="filter-apply"
        @click="handleApply"
      >
        查询
      </el-button>
      <el-button size="small" data-test="filter-reset" @click="handleReset">
        重置
      </el-button>
    </div>
  </section>
</template>

<style scoped>
.filter-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-end;
  justify-content: space-between;
  gap: 12px 16px;
  padding: 14px 16px;
  border: 1px solid #eef1f6;
  border-radius: 10px;
  background: #ffffff;
}

.filter-bar__fields {
  display: grid;
  flex: 1 1 720px;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 10px 12px;
}

.filter-bar__field {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.filter-bar__field--wide {
  grid-column: span 2;
}

.filter-bar__label {
  color: #6b7280;
  font-size: 12px;
}

.filter-bar__toggles {
  display: flex;
  align-items: center;
  gap: 18px;
  padding-bottom: 4px;
}

.filter-bar__toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #4b5563;
  font-size: 12px;
}

.filter-bar__actions {
  display: flex;
  gap: 8px;
}

@media (max-width: 960px) {
  .filter-bar__field--wide {
    grid-column: span 1;
  }
}
</style>

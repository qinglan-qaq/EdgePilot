<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import type { ToolAnalyticsItem } from '@/api/observability'
import AsyncState from '@/components/common/AsyncState.vue'
import MetricCard from '@/components/observability/MetricCard.vue'
import { useObservabilityStore } from '@/stores/observability'
import { formatDurationMs } from '@/utils/status'
import {
  RANGE_OPTIONS,
  rangeQuery,
  type RangePreset,
} from '@/utils/observabilityRange'

const router = useRouter()
const store = useObservabilityStore()
const range = ref<RangePreset>('24h')

const items = computed<ToolAnalyticsItem[]>(() => store.analyticsTools ?? [])

function load(): void {
  void store.loadAnalyticsTools(rangeQuery(range.value))
}

onMounted(load)

function percentText(value: number | null): string {
  return value === null ? '未上报' : `${(value * 100).toFixed(1)}%`
}

function durationText(value: number | null): string {
  return value === null ? '未上报' : formatDurationMs(value)
}

/**
 * 汇总卡全部由真实行派生：
 * - 成功率按各工具调用数加权，仅计入上报了成功率的工具；
 * - P95 展示各工具 P95 的最大值（不同工具的分位数不能相加）。
 */
const summary = computed(() => {
  const list = items.value
  const calls = list.reduce((sum, item) => sum + item.calls, 0)
  const permissionDenied = list.reduce(
    (sum, item) => sum + item.permission_denied,
    0,
  )

  let weighted = 0
  let weight = 0
  for (const item of list) {
    if (item.success_rate !== null) {
      weighted += item.success_rate * item.calls
      weight += item.calls
    }
  }

  const p95Values = list
    .map((item) => item.p95_ms)
    .filter((value): value is number => value !== null)

  return {
    calls,
    permissionDenied,
    successRate: weight > 0 ? weighted / weight : null,
    p95: p95Values.length > 0 ? Math.max(...p95Values) : null,
  }
})

/** 只有存在 tool_id 的行才跳转 Trace 查询。 */
function openTool(row: ToolAnalyticsItem): void {
  if (!row.tool_id) {
    return
  }
  void router.push({
    name: 'observability-traces',
    query: { tool: row.tool_id },
  })
}
</script>

<template>
  <section class="analytics-page">
    <header class="analytics-page__toolbar">
      <div>
        <h2 class="analytics-page__title">工具分析</h2>
        <p class="analytics-page__subtitle">
          数据来自真实 analytics API；逻辑 tool span（attempt=1）口径，点击行可查看对应 Trace。
        </p>
      </div>

      <div class="analytics-page__actions">
        <el-radio-group
          v-model="range"
          size="small"
          data-test="tools-range"
          @change="load"
        >
          <el-radio-button
            v-for="option in RANGE_OPTIONS"
            :key="option.value"
            :value="option.value"
          >
            {{ option.label }}
          </el-radio-button>
        </el-radio-group>

        <el-button size="small" :loading="store.analyticsToolsLoading" @click="load">
          刷新
        </el-button>
      </div>
    </header>

    <AsyncState
      :loading="store.analyticsToolsLoading"
      :error="store.analyticsToolsError"
      :empty="items.length === 0"
      @retry="load"
    >
      <template #empty>
        <el-empty description="时间窗内没有工具调用记录" />
      </template>

      <div class="analytics-page__cards">
        <MetricCard
          metric="tools_calls"
          label="调用数"
          :value="String(summary.calls)"
          hint="时间窗内逻辑 tool span（attempt=1）的调用总数。"
        />
        <MetricCard
          metric="tools_success_rate"
          label="成功率"
          :value="percentText(summary.successRate)"
          hint="按各工具调用数加权的成功率；仅统计上报了成功率的工具，无数据时显示未上报。"
          :progress="summary.successRate"
        />
        <MetricCard
          metric="tools_p95"
          label="P95 延迟"
          :value="durationText(summary.p95)"
          hint="各工具 P95 耗时中的最大值（分位数不能跨工具相加）；无耗时数据显示未上报。"
        />
        <MetricCard
          metric="tools_permission_denied"
          label="权限拒绝"
          :value="String(summary.permissionDenied)"
          hint="tool_calls.permission_result='denied' 的调用次数总和。"
        />
      </div>

      <el-card shadow="never" class="analytics-page__table-card">
        <el-table
          :data="items"
          row-key="tool_id"
          size="small"
          data-test="tools-table"
          @row-click="openTool"
        >
          <el-table-column prop="tool_id" label="tool_id" min-width="160" />
          <el-table-column label="工具名称" min-width="150">
            <template #default="{ row }">
              {{ row.tool_name ?? '未记录' }}
            </template>
          </el-table-column>
          <el-table-column prop="calls" label="调用数" width="90" />
          <el-table-column label="成功率" width="110">
            <template #default="{ row }">
              {{ percentText(row.success_rate) }}
            </template>
          </el-table-column>
          <el-table-column label="P50" width="110">
            <template #default="{ row }">
              {{ durationText(row.p50_ms) }}
            </template>
          </el-table-column>
          <el-table-column label="P95" width="110">
            <template #default="{ row }">
              {{ durationText(row.p95_ms) }}
            </template>
          </el-table-column>
          <el-table-column prop="retry_count" label="重试" width="80" />
          <el-table-column prop="permission_denied" label="拒绝" width="80" />
        </el-table>
      </el-card>
    </AsyncState>
  </section>
</template>

<style scoped>
.analytics-page {
  width: 100%;
}

.analytics-page__toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
  padding: 18px 20px;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.analytics-page__title {
  margin: 0;
  color: #111827;
  font-size: 18px;
  font-weight: 600;
}

.analytics-page__subtitle {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.6;
}

.analytics-page__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
}

.analytics-page__cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
}

.analytics-page__table-card {
  margin-top: 16px;
  border-radius: 12px;
}

.analytics-page__table-card :deep(.el-table__row) {
  cursor: pointer;
}

@media (max-width: 960px) {
  .analytics-page__toolbar {
    flex-direction: column;
  }

  .analytics-page__actions {
    justify-content: flex-start;
  }
}
</style>

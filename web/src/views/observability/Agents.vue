<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import type { AgentAnalyticsItem } from '@/api/observability'
import AsyncState from '@/components/common/AsyncState.vue'
import MetricCard from '@/components/observability/MetricCard.vue'
import { useObservabilityStore } from '@/stores/observability'
import { formatDurationMs, formatTokens } from '@/utils/status'
import {
  RANGE_OPTIONS,
  rangeQuery,
  type RangePreset,
} from '@/utils/observabilityRange'

const router = useRouter()
const store = useObservabilityStore()
const range = ref<RangePreset>('24h')

const items = computed<AgentAnalyticsItem[]>(() => store.analyticsAgents ?? [])

function load(): void {
  void store.loadAnalyticsAgents(rangeQuery(range.value))
}

onMounted(load)

function percentText(value: number | null): string {
  return value === null ? '未上报' : `${(value * 100).toFixed(1)}%`
}

function durationText(value: number | null): string {
  return value === null ? '未上报' : formatDurationMs(value)
}

/**
 * 汇总卡由真实行派生：
 * - 成功率按各 agent 执行数加权，仅计入上报了成功率的 agent；
 * - P95 展示各 agent P95 的最大值（分位数不能跨 agent 相加）。
 */
const summary = computed(() => {
  const list = items.value
  const runs = list.reduce((sum, item) => sum + item.runs, 0)

  let weighted = 0
  let weight = 0
  for (const item of list) {
    if (item.success_rate !== null) {
      weighted += item.success_rate * item.runs
      weight += item.runs
    }
  }

  const p95Values = list
    .map((item) => item.p95_duration_ms)
    .filter((value): value is number => value !== null)

  return {
    agentCount: list.length,
    runs,
    successRate: weight > 0 ? weighted / weight : null,
    p95: p95Values.length > 0 ? Math.max(...p95Values) : null,
  }
})

/** agent_id 为数字时才跳转 Trace 查询；空值不构造假链接。 */
function openAgent(row: AgentAnalyticsItem): void {
  if (row.agent_id === null) {
    return
  }
  void router.push({
    name: 'observability-traces',
    query: { agent_id: String(row.agent_id) },
  })
}

/** 同名 Agent 可能对应多个 agent_id，row-key 必须带 id 才不会重复。 */
function agentRowKey(row: AgentAnalyticsItem): string {
  return `${row.agent_id ?? 'null'}:${row.label ?? ''}`
}
</script>

<template>
  <section class="analytics-page">
    <header class="analytics-page__toolbar">
      <div>
        <h2 class="analytics-page__title">Agent 分析</h2>
        <p class="analytics-page__subtitle">
          Agent 执行数、成功率、耗时与工具使用；逻辑 agent span（attempt=1）口径。
        </p>
      </div>

      <div class="analytics-page__actions">
        <el-radio-group
          v-model="range"
          size="small"
          data-test="agents-range"
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

        <el-button size="small" :loading="store.analyticsAgentsLoading" @click="load">
          刷新
        </el-button>
      </div>
    </header>

    <AsyncState
      :loading="store.analyticsAgentsLoading"
      :error="store.analyticsAgentsError"
      :empty="items.length === 0"
      @retry="load"
    >
      <template #empty>
        <el-empty description="时间窗内没有 Agent 执行记录" />
      </template>

      <div class="analytics-page__cards">
        <MetricCard
          metric="agents_count"
          label="Agent 数"
          :value="String(summary.agentCount)"
          hint="时间窗内出现执行记录的 Agent 数量。"
        />
        <MetricCard
          metric="agents_runs"
          label="执行数"
          :value="String(summary.runs)"
          hint="逻辑 agent span 数（span_type='agent' 且 attempt=1），重试不计入。"
        />
        <MetricCard
          metric="agents_success_rate"
          label="成功率"
          :value="percentText(summary.successRate)"
          hint="按各 Agent 执行数加权的成功率；仅统计上报了成功率的 Agent。"
          :progress="summary.successRate"
        />
        <MetricCard
          metric="agents_p95"
          label="P95 耗时"
          :value="durationText(summary.p95)"
          hint="各 Agent P95 耗时中的最大值（分位数不能跨 Agent 相加）；无数据显示未上报。"
        />
      </div>

      <el-card shadow="never" class="analytics-page__table-card">
        <el-table
          :data="items"
          :row-key="agentRowKey"
          size="small"
          data-test="agents-table"
          @row-click="openAgent"
        >
          <el-table-column label="Agent" min-width="180">
            <template #default="{ row }">{{ row.label ?? '未记录' }}</template>
          </el-table-column>
          <el-table-column label="agent_id" width="110">
            <template #default="{ row }">{{ row.agent_id ?? '未记录' }}</template>
          </el-table-column>
          <el-table-column prop="runs" label="执行数" width="90" />
          <el-table-column prop="failed" label="失败" width="80" />
          <el-table-column label="成功率" width="110">
            <template #default="{ row }">
              {{ percentText(row.success_rate) }}
            </template>
          </el-table-column>
          <el-table-column label="平均耗时" width="120">
            <template #default="{ row }">
              {{ durationText(row.avg_duration_ms) }}
            </template>
          </el-table-column>
          <el-table-column label="P95" width="110">
            <template #default="{ row }">
              {{ durationText(row.p95_duration_ms) }}
            </template>
          </el-table-column>
          <el-table-column label="Token" width="120">
            <template #default="{ row }">{{ formatTokens(row.total_tokens) }}</template>
          </el-table-column>
          <el-table-column prop="tool_calls" label="工具调用" width="100" />
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

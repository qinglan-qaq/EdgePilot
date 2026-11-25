<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import type { ModelAnalyticsItem } from '@/api/observability'
import AsyncState from '@/components/common/AsyncState.vue'
import MetricCard from '@/components/observability/MetricCard.vue'
import { useObservabilityStore } from '@/stores/observability'
import { formatCost, formatTokens } from '@/utils/status'
import {
  RANGE_OPTIONS,
  rangeQuery,
  type RangePreset,
} from '@/utils/observabilityRange'

const router = useRouter()
const store = useObservabilityStore()
const range = ref<RangePreset>('24h')

const items = computed<ModelAnalyticsItem[]>(() => store.analyticsModels ?? [])

function load(): void {
  void store.loadAnalyticsModels(rangeQuery(range.value))
}

onMounted(load)

interface ModelCostRow {
  cost?: number | null
  cost_unavailable_count?: number
}

/** 成本展示：null 明确显示“费用不可用”，绝不用 0 冒充。 */
function costText(row: ModelCostRow): string {
  const cost = row.cost ?? null
  const unavailable = row.cost_unavailable_count ?? 0
  if (cost === null) {
    return '费用不可用'
  }
  if (unavailable > 0) {
    return `${formatCost(cost)}（部分未配价）`
  }
  return formatCost(cost)
}

/** 汇总卡：聚合真实行；全部缺价时总成本保持 null。 */
const summary = computed(() => {
  const list = items.value
  const calls = list.reduce((sum, item) => sum + item.calls, 0)
  const errors = list.reduce((sum, item) => sum + item.errors, 0)
  const costUnavailable = list.reduce(
    (sum, item) => sum + item.cost_unavailable_count,
    0,
  )

  let tokenTotal: number | null = null
  let costTotal: number | null = null
  for (const item of list) {
    if (item.total_tokens !== null) {
      tokenTotal = (tokenTotal ?? 0) + item.total_tokens
    }
    if (item.cost !== null) {
      costTotal = (costTotal ?? 0) + item.cost
    }
  }

  return { calls, errors, costUnavailable, tokenTotal, costTotal }
})

const tokenText = computed(() => formatTokens(summary.value.tokenTotal))

const costValue = computed(() => {
  if (summary.value.costTotal === null) {
    return '费用不可用'
  }
  return formatCost(summary.value.costTotal)
})

const costDetail = computed(() => {
  if (summary.value.costUnavailable > 0) {
    return `${summary.value.costUnavailable} 次调用未配价，成本仅统计已配价调用`
  }
  return null
})

/** model_name 存在时跳转 Trace 查询。 */
function openModel(row: ModelAnalyticsItem): void {
  if (!row.model_name) {
    return
  }
  void router.push({
    name: 'observability-traces',
    query: { model: row.model_name },
  })
}

/** provider + model 才是唯一键；不同 provider 可能有同名 model。 */
function modelRowKey(row: ModelAnalyticsItem): string {
  return `${row.provider ?? 'null'}:${row.model_name ?? 'null'}`
}
</script>

<template>
  <section class="analytics-page">
    <header class="analytics-page__toolbar">
      <div>
        <h2 class="analytics-page__title">模型分析</h2>
        <p class="analytics-page__subtitle">
          provider / model 维度的真实调用、Token、成本与错误统计；缺失成本不做估算。
        </p>
      </div>

      <div class="analytics-page__actions">
        <el-radio-group
          v-model="range"
          size="small"
          data-test="models-range"
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

        <el-button size="small" :loading="store.analyticsModelsLoading" @click="load">
          刷新
        </el-button>
      </div>
    </header>

    <AsyncState
      :loading="store.analyticsModelsLoading"
      :error="store.analyticsModelsError"
      :empty="items.length === 0"
      @retry="load"
    >
      <template #empty>
        <el-empty description="时间窗内没有模型调用记录" />
      </template>

      <div class="analytics-page__cards">
        <MetricCard
          metric="models_calls"
          label="调用数"
          :value="String(summary.calls)"
          hint="时间窗内 llm_calls 的调用总数，按 created_at 统计。"
        />
        <MetricCard
          metric="models_tokens"
          label="Token 总量"
          :value="tokenText"
          hint="sum(llm_calls.total_tokens)；全部调用缺 usage 时显示未上报。"
        />
        <MetricCard
          metric="models_cost"
          label="总成本"
          :value="costValue"
          :detail="costDetail"
          hint="sum(llm_calls.cost)；缺价调用只计入缺价次数，绝不估算。"
        />
        <MetricCard
          metric="models_errors"
          label="错误数"
          :value="String(summary.errors)"
          hint="llm_calls.error 非空（且非 JSON null）的调用数。"
        />
      </div>

      <el-card shadow="never" class="analytics-page__table-card">
        <el-table
          :data="items"
          :row-key="modelRowKey"
          size="small"
          data-test="models-table"
          @row-click="openModel"
        >
          <el-table-column label="provider" min-width="120">
            <template #default="{ row }">{{ row.provider ?? '未记录' }}</template>
          </el-table-column>
          <el-table-column label="model" min-width="150">
            <template #default="{ row }">{{ row.model_name ?? '未记录' }}</template>
          </el-table-column>
          <el-table-column prop="calls" label="调用数" width="90" />
          <el-table-column label="prompt tokens" width="130">
            <template #default="{ row }">{{ formatTokens(row.prompt_tokens) }}</template>
          </el-table-column>
          <el-table-column label="completion tokens" width="150">
            <template #default="{ row }">
              {{ formatTokens(row.completion_tokens) }}
            </template>
          </el-table-column>
          <el-table-column label="cached tokens" width="130">
            <template #default="{ row }">{{ formatTokens(row.cached_tokens) }}</template>
          </el-table-column>
          <el-table-column label="total tokens" width="130">
            <template #default="{ row }">{{ formatTokens(row.total_tokens) }}</template>
          </el-table-column>
          <el-table-column label="cost" min-width="160">
            <template #default="{ row }">
              <span data-test="model-cost">{{ costText(row) }}</span>
            </template>
          </el-table-column>
          <el-table-column label="缺价次数" width="100">
            <template #default="{ row }">{{ row.cost_unavailable_count }}</template>
          </el-table-column>
          <el-table-column label="errors" width="90">
            <template #default="{ row }">{{ row.errors }}</template>
          </el-table-column>
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

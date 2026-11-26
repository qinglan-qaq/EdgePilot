<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import type { AnalyticsQuery } from '@/api/observability'
import AsyncState from '@/components/common/AsyncState.vue'
import BarList from '@/components/observability/BarList.vue'
import MetricCard from '@/components/observability/MetricCard.vue'
import TrendChart from '@/components/observability/TrendChart.vue'
import type { BarListItem } from '@/components/observability/chart-types'
import { useObservabilityStore } from '@/stores/observability'
import { formatDateTime } from '@/utils/datetime'
import {
  formatCost,
  formatDurationMs,
  formatTokens,
  runStatusLabel,
  runStatusTagType,
} from '@/utils/status'

type RangePreset = '1h' | '24h' | '7d' | '30d'

interface RangeOption {
  value: RangePreset
  label: string
}

interface CardView {
  metric: string
  label: string
  value: string
  hint: string
  detail?: string
  progress?: number | null
}

const RANGE_OPTIONS: RangeOption[] = [
  { value: '1h', label: '近 1 小时' },
  { value: '24h', label: '近 24 小时' },
  { value: '7d', label: '近 7 天' },
  { value: '30d', label: '近 30 天' },
]

const RANGE_HOURS: Record<Exclude<RangePreset, '24h'>, number> = {
  '1h': 1,
  '7d': 24 * 7,
  '30d': 24 * 30,
}

const store = useObservabilityStore()
const range = ref<RangePreset>('24h')

/** null 一律显示“未上报”，绝不用 0 代替。 */
function durationText(value: number | null): string {
  return value === null ? '未上报' : formatDurationMs(value)
}

function percentText(value: number | null): string {
  return value === null ? '未上报' : `${(value * 100).toFixed(1)}%`
}

function tokenText(value: number | null): string {
  return formatTokens(value)
}

function formatMsValue(value: number | null): string {
  return durationText(value)
}

function formatPercentValue(value: number | null): string {
  return percentText(value)
}

/**
 * 近 24 小时不传任何参数，由后端解析为最近 24 小时；
 * 其他预设只传 ISO start_from，结束时间交给后端取当前时刻。
 */
function rangeQuery(preset: RangePreset): AnalyticsQuery {
  if (preset === '24h') {
    return {}
  }
  const hours = RANGE_HOURS[preset]
  return { start_from: new Date(Date.now() - hours * 3600_000).toISOString() }
}

/**
 * 按当前时间预设加载概览。
 *
 * 关键节点：24 小时走后端默认窗口；其他预设只传 start_from，
 * 结束时间由后端取当前时刻，避免前后端时钟偏差。
 */
function load(): void {
  void store.loadOverview(rangeQuery(range.value))
}

/** 切换时间范围后立即重载；旧响应由 store 的单调令牌丢弃。 */
function handleRangeChange(): void {
  load()
}

onMounted(load)

const cards = computed<CardView[]>(() => {
  const data = store.overview?.cards
  if (!data) {
    return []
  }
  const coverage = data.cost_coverage
  return [
    {
      metric: 'traces_total',
      label: 'Trace 总数',
      value: String(data.traces_total),
      hint: '时间窗内 traces 表记录数，按 trace.start_time 统计，包含运行中的 trace。',
    },
    {
      metric: 'trace_success_rate',
      label: 'Trace 成功率',
      value: percentText(data.trace_success_rate),
      hint: 'succeeded trace 数 / 已结束 trace 数（排除 running）；无已结束 trace 时显示未上报。',
      progress: data.trace_success_rate,
    },
    {
      metric: 'trace_p95_ms',
      label: 'Trace P95 延迟',
      value: durationText(data.trace_p95_ms),
      hint: '时间窗内 trace duration_ms 的 95 分位（percentile_cont）；无耗时数据显示未上报。',
    },
    {
      metric: 'spans_total',
      label: 'Span 总数',
      value: String(data.spans_total),
      hint: '时间窗内 spans 表记录数，按 span.start_time 统计，包含所有 attempt。',
    },
    {
      metric: 'agent_runs',
      label: 'Agent 执行数',
      value: String(data.agent_runs),
      hint: "逻辑 agent span 数（span_type='agent' 且 attempt=1），重试不计入。",
    },
    {
      metric: 'tool_success_rate',
      label: '工具成功率',
      value: percentText(data.tool_success_rate),
      hint: '已结束逻辑 tool span 中 succeeded 占比（attempt=1，排除 running）。',
      progress: data.tool_success_rate,
    },
    {
      metric: 'tool_permission_denied',
      label: '权限拒绝数',
      value: String(data.tool_permission_denied),
      hint: "tool_calls.permission_result='denied' 的调用次数。",
    },
    {
      metric: 'llm_calls',
      label: 'LLM 调用数',
      value: String(data.llm_calls),
      hint: '时间窗内 llm_calls 表记录数，按 created_at 统计。',
    },
    {
      metric: 'total_tokens',
      label: 'Token 总量',
      value: tokenText(data.total_tokens),
      hint: 'sum(llm_calls.total_tokens)；全部调用缺 usage 时显示未上报，绝不用 0 代替。',
    },
    {
      metric: 'total_cost',
      label: '总成本',
      value: formatCost(data.total_cost),
      detail: `覆盖 ${coverage.priced_models}/${coverage.total_models} 模型 · ${data.cost_unavailable_count} 次调用缺价`,
      hint: 'sum(llm_calls.cost)；缺价调用只计入缺价次数，绝不估算。覆盖比例 = 有单价模型数 / 涉及模型数。',
    },
  ]
})

const windowLabel = computed(() => {
  const window = store.overview?.window
  if (!window) {
    return ''
  }
  const bucket = window.bucket === 'hour' ? '按小时分桶' : '按天分桶'
  return `${formatDateTime(window.start_from)} — ${formatDateTime(window.start_to)} · ${bucket}`
})

const bucket = computed(() => store.overview?.window.bucket ?? 'hour')

function formatBucketLabel(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) {
    return value
  }
  const pad = (input: number): string => String(input).padStart(2, '0')
  const monthDay = `${pad(date.getMonth() + 1)}-${pad(date.getDate())}`
  if (bucket.value === 'day') {
    return monthDay
  }
  return `${monthDay} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}

const traceTrendSeries = [
  { key: 'total', label: '总数', color: '#2563eb' },
  { key: 'succeeded', label: '成功', color: '#16a34a' },
  { key: 'failed', label: '失败', color: '#dc2626' },
]

const latencyTrendSeries = [
  { key: 'p50_ms', label: 'P50', color: '#2563eb' },
  { key: 'p95_ms', label: 'P95', color: '#f97316' },
]

const tokenTrendSeries = [
  { key: 'total_tokens', label: '总 Token', color: '#2563eb' },
  { key: 'cached_tokens', label: '缓存 Token', color: '#0891b2' },
]

const traceTrendPoints = computed(() =>
  (store.overview?.charts.trace_trend ?? []).map((point) => ({
    label: formatBucketLabel(point.bucket),
    values: {
      total: point.total,
      succeeded: point.succeeded,
      failed: point.failed,
    },
  })),
)

const latencyTrendPoints = computed(() =>
  (store.overview?.charts.latency_trend ?? []).map((point) => ({
    label: formatBucketLabel(point.bucket),
    values: {
      p50_ms: point.p50_ms,
      p95_ms: point.p95_ms,
    },
  })),
)

const tokenTrendPoints = computed(() =>
  (store.overview?.charts.token_trend ?? []).map((point) => ({
    label: formatBucketLabel(point.bucket),
    values: {
      total_tokens: point.total_tokens,
      cached_tokens: point.cached_tokens,
    },
  })),
)

const modelItems = computed<BarListItem[]>(() =>
  (store.overview?.charts.model_distribution ?? []).map((item) => {
    const costDetail =
      item.cost_unavailable_count > 0 ? ` · ${item.cost_unavailable_count} 次缺价` : ''
    return {
      label: `${item.provider ?? '未知 provider'} / ${item.model_name ?? '未知模型'}`,
      value: item.calls,
      detail: `Token ${formatTokens(item.total_tokens)} · 成本 ${formatCost(item.cost)}${costDetail}`,
    }
  }),
)

const agentItems = computed<BarListItem[]>(() =>
  (store.overview?.charts.agent_distribution ?? []).map((item) => ({
    label: item.label ?? '未知 Agent',
    value: item.runs,
    detail: `失败 ${item.failed} · P95 ${durationText(item.p95_ms)}`,
    tone: item.failed > 0 ? 'warning' : 'success',
  })),
)

const toolItems = computed<BarListItem[]>(() =>
  (store.overview?.charts.tool_distribution ?? []).map((item) => ({
    label: item.tool_name ?? item.tool_id,
    value: item.success_rate,
    detail: `调用 ${item.calls} · P95 ${durationText(item.p95_ms)}`,
    tone:
      item.success_rate === null
        ? 'info'
        : item.success_rate >= 0.95
          ? 'success'
          : item.success_rate >= 0.8
            ? 'warning'
            : 'danger',
  })),
)

const errorItems = computed<BarListItem[]>(() =>
  (store.overview?.charts.error_distribution ?? []).map((item) => ({
    label: item.code,
    value: item.count,
    tone: 'danger',
  })),
)

const slowestTraces = computed(() => store.overview?.charts.slowest_traces ?? [])
</script>

<template>
  <section class="overview">
    <header class="overview__toolbar">
      <div class="overview__intro">
        <h2 class="overview__title">可观测概览</h2>
        <p class="overview__subtitle">
          全部数据来自真实 analytics API；每个指标卡与图表标题都标注了统计口径。
        </p>
        <p v-if="windowLabel" class="overview__window" data-test="overview-window">
          统计窗口：{{ windowLabel }}
        </p>
      </div>

      <div class="overview__actions">
        <el-radio-group
          v-model="range"
          size="small"
          data-test="overview-range"
          @change="handleRangeChange"
        >
          <el-radio-button
            v-for="option in RANGE_OPTIONS"
            :key="option.value"
            :value="option.value"
          >
            {{ option.label }}
          </el-radio-button>
        </el-radio-group>

        <el-button
          size="small"
          :loading="store.loading"
          data-test="overview-refresh"
          @click="load"
        >
          刷新
        </el-button>
      </div>
    </header>

    <AsyncState :loading="store.loading" :error="store.error" @retry="load">
      <template v-if="store.overview">
        <div class="overview__cards">
          <MetricCard v-for="card in cards" :key="card.metric" v-bind="card" />
        </div>

        <div class="overview__charts">
          <el-card shadow="never" class="overview__chart-card">
            <template #header>
              <div class="overview__chart-header">
                <h3>Trace 趋势</h3>
                <p>
                  按 trace 开始时间分桶统计 total / succeeded / failed；失败含
                  failed、timeout、cancelled。
                </p>
              </div>
            </template>
            <TrendChart
              :points="traceTrendPoints"
              :series="traceTrendSeries"
              kind="line"
            />
          </el-card>

          <el-card shadow="never" class="overview__chart-card">
            <template #header>
              <div class="overview__chart-header">
                <h3>延迟趋势</h3>
                <p>按分桶计算 trace duration_ms 的 P50 / P95；无耗时数据的分桶留空。</p>
              </div>
            </template>
            <TrendChart
              :points="latencyTrendPoints"
              :series="latencyTrendSeries"
              kind="line"
              :value-formatter="formatMsValue"
            />
          </el-card>

          <el-card shadow="never" class="overview__chart-card">
            <template #header>
              <div class="overview__chart-header">
                <h3>Token 趋势</h3>
                <p>
                  按分桶汇总 llm_calls.total_tokens 与 cached_tokens；缺 usage 时留空，不按
                  0 处理。
                </p>
              </div>
            </template>
            <TrendChart
              :points="tokenTrendPoints"
              :series="tokenTrendSeries"
              kind="bar"
              :value-formatter="tokenText"
            />
          </el-card>

          <el-card shadow="never" class="overview__chart-card">
            <template #header>
              <div class="overview__chart-header">
                <h3>模型分布</h3>
                <p>按 provider + model_name 分组统计调用次数、token 与成本；缺价调用单列。</p>
              </div>
            </template>
            <BarList :items="modelItems" />
          </el-card>

          <el-card shadow="never" class="overview__chart-card">
            <template #header>
              <div class="overview__chart-header">
                <h3>Agent 分布</h3>
                <p>逻辑 agent span（attempt=1）执行数；P95 为该 agent span 耗时 95 分位。</p>
              </div>
            </template>
            <BarList :items="agentItems" />
          </el-card>

          <el-card shadow="never" class="overview__chart-card">
            <template #header>
              <div class="overview__chart-header">
                <h3>工具成功率</h3>
                <p>
                  逻辑 tool span（attempt=1）成功率 = succeeded / 已结束；P95 为工具耗时 95
                  分位。
                </p>
              </div>
            </template>
            <BarList
              :items="toolItems"
              :max="1"
              :value-formatter="formatPercentValue"
            />
          </el-card>

          <el-card shadow="never" class="overview__chart-card">
            <template #header>
              <div class="overview__chart-header">
                <h3>错误分布</h3>
                <p>按 spans.error.code 分组；缺失错误码归入 unknown。</p>
              </div>
            </template>
            <BarList :items="errorItems" />
          </el-card>

          <el-card shadow="never" class="overview__chart-card overview__chart-card--wide">
            <template #header>
              <div class="overview__chart-header">
                <h3>最慢 Trace</h3>
                <p>时间窗内 trace duration_ms 降序 Top 10。</p>
              </div>
            </template>

            <p v-if="slowestTraces.length === 0" class="overview__empty">暂无数据</p>
            <el-table v-else :data="slowestTraces" row-key="trace_id" size="small">
              <el-table-column label="Trace 名称" min-width="200">
                <template #default="{ row }">
                  <span class="overview__trace-name" :title="row.trace_id">
                    {{ row.name ?? row.trace_id }}
                  </span>
                </template>
              </el-table-column>

              <el-table-column label="状态" width="110">
                <template #default="{ row }">
                  <el-tag :type="runStatusTagType(row.status)" effect="light" size="small">
                    {{ runStatusLabel(row.status) }}
                  </el-tag>
                </template>
              </el-table-column>

              <el-table-column label="耗时" width="120">
                <template #default="{ row }">
                  <span>{{ durationText(row.duration_ms) }}</span>
                </template>
              </el-table-column>

              <el-table-column label="开始时间" width="160">
                <template #default="{ row }">
                  <span class="overview__muted">{{ formatDateTime(row.start_time) }}</span>
                </template>
              </el-table-column>
            </el-table>
          </el-card>
        </div>
      </template>
    </AsyncState>
  </section>
</template>

<style scoped>
.overview {
  width: 100%;
}

.overview__toolbar {
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

.overview__title {
  margin: 0;
  color: #111827;
  font-size: 18px;
  font-weight: 600;
}

.overview__subtitle {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.6;
}

.overview__window {
  margin: 6px 0 0;
  color: #94a3b8;
  font-size: 12px;
}

.overview__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
}

.overview__cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
}

.overview__charts {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin-top: 16px;
}

.overview__chart-card {
  border-radius: 12px;
}

.overview__chart-card--wide {
  grid-column: 1 / -1;
}

.overview__chart-header h3 {
  margin: 0;
  color: #111827;
  font-size: 15px;
  font-weight: 600;
}

.overview__chart-header p {
  margin: 6px 0 0;
  color: #94a3b8;
  font-size: 12px;
  line-height: 1.6;
}

.overview__trace-name {
  font-weight: 500;
}

.overview__muted {
  color: #94a3b8;
  font-size: 13px;
}

.overview__empty {
  margin: 0;
  padding: 32px 0;
  color: #94a3b8;
  font-size: 13px;
  text-align: center;
}

@media (max-width: 960px) {
  .overview__toolbar {
    flex-direction: column;
  }

  .overview__actions {
    justify-content: flex-start;
  }

  .overview__charts {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>

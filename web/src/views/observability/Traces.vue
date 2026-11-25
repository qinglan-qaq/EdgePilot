<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import type { TraceSummary } from '@/api/observability'
import AsyncState from '@/components/common/AsyncState.vue'
import FilterBar from '@/components/observability/FilterBar.vue'
import { useObservabilityStore } from '@/stores/observability'
import { formatDateTime } from '@/utils/datetime'
import {
  formatCost,
  formatDurationMs,
  formatTokens,
  runStatusLabel,
  runStatusTagType,
} from '@/utils/status'
import {
  buildTraceListParams,
  buildTraceRouteQuery,
  EMPTY_TRACE_FILTERS,
  parseTraceFilters,
  parseTracePage,
  type TraceFilterState,
} from '@/utils/traceFilters'

/** 每页 100 条：配合固定行高做窗口化，避免一次渲染整页 DOM。 */
const PAGE_SIZE = 100

/** 虚拟滚动常量；行高必须与 CSS 中 .traces__row 的 height 一致。 */
const ROW_HEIGHT = 44
const VIEWPORT_HEIGHT = 480
const OVERSCAN = 6

const route = useRoute()
const router = useRouter()
const store = useObservabilityStore()

// URL 是筛选条件的唯一来源；local draft 只承载“尚未点击查询”的编辑值。
const filters = ref<TraceFilterState>(parseTraceFilters(route.query))
const page = computed(() => parseTracePage(route.query))

const viewport = ref<HTMLElement | null>(null)
const scrollTop = ref(0)

const itemCount = computed(() => store.traces.length)
const empty = computed(() => itemCount.value === 0)
const spacerHeight = computed(() => itemCount.value * ROW_HEIGHT)

const visibleStart = computed(() => {
  if (itemCount.value === 0) {
    return 0
  }
  return Math.max(0, Math.floor(scrollTop.value / ROW_HEIGHT) - OVERSCAN)
})

const visibleEnd = computed(() =>
  Math.min(
    itemCount.value,
    visibleStart.value + Math.ceil(VIEWPORT_HEIGHT / ROW_HEIGHT) + OVERSCAN * 2,
  ),
)

const visibleRows = computed<TraceSummary[]>(() =>
  store.traces.slice(visibleStart.value, visibleEnd.value),
)

const offsetTop = computed(() => visibleStart.value * ROW_HEIGHT)

function reload(): void {
  void store.loadTraces(
    buildTraceListParams(filters.value, page.value, PAGE_SIZE),
  )
}

watch(
  () => route.query,
  () => {
    // 浏览器前进/后退、直接修改 URL 或点击查询后，都回到 URL 重新解析。
    filters.value = parseTraceFilters(route.query)
    scrollTop.value = 0
    if (viewport.value) {
      viewport.value.scrollTop = 0
    }
    reload()
  },
  { immediate: true },
)

function queryValue(value: unknown): string {
  if (Array.isArray(value)) {
    return value.length > 0 ? String(value[0]) : ''
  }
  return value === null || value === undefined ? '' : String(value)
}

/** 当前 URL query 与目标是否一致；一致时 router.replace 不会触发 watcher。 */
function queryEquals(target: Record<string, string>): boolean {
  const currentKeys = Object.keys(route.query).filter(
    (key) => queryValue(route.query[key]) !== '',
  )
  const targetKeys = Object.keys(target)
  if (currentKeys.length !== targetKeys.length) {
    return false
  }
  return targetKeys.every((key) => queryValue(route.query[key]) === target[key])
}

/** 写回 URL；同 query 点击查询/重置时显式重载，保证用户仍能得到最新数据。 */
async function navigateQuery(target: Record<string, string>): Promise<void> {
  if (queryEquals(target)) {
    reload()
    return
  }
  await router.replace({ query: target })
}

function handleFilterUpdate(value: TraceFilterState): void {
  filters.value = value
}

function handleApply(): void {
  // 应用筛选必须回到第 1 页，避免落在超出新结果集的页码。
  void navigateQuery(buildTraceRouteQuery(filters.value, 1))
}

function handleReset(): void {
  filters.value = { ...EMPTY_TRACE_FILTERS }
  void navigateQuery({})
}

function handlePageChange(nextPage: number): void {
  // 翻页沿用 URL 中已生效的筛选，不把用户尚未点击“查询”的编辑值带上。
  const applied = parseTraceFilters(route.query)
  void navigateQuery(buildTraceRouteQuery(applied, nextPage))
}

function handleScroll(event: Event): void {
  const target = event.currentTarget as HTMLElement | null
  scrollTop.value = target?.scrollTop ?? 0
}

function openTrace(traceId: string): void {
  void router.push({
    name: 'observability-trace-detail',
    params: { traceId },
  })
}

/** null 表示后端未上报耗时，不能显示成 0。 */
function durationText(value: number | null): string {
  return value === null ? '未上报' : formatDurationMs(value)
}

function traceName(trace: TraceSummary): string {
  return trace.name.trim() || '未命名 Trace'
}
</script>

<template>
  <section class="traces">
    <header class="traces__header">
      <div>
        <h2 class="traces__title">Trace Explorer</h2>
        <p class="traces__subtitle">
          筛选条件全部写入 URL 并由后端执行；刷新或分享链接会还原同一组条件，
          前端不会拉取全量 Trace 再过滤。
        </p>
      </div>
      <el-button
        size="small"
        :loading="store.tracesLoading"
        data-test="traces-refresh"
        @click="reload"
      >
        刷新
      </el-button>
    </header>

    <FilterBar
      :model-value="filters"
      :loading="store.tracesLoading"
      @update:model-value="handleFilterUpdate"
      @apply="handleApply"
      @reset="handleReset"
    />

    <div class="traces__body">
      <AsyncState
        :loading="store.tracesLoading"
        :error="store.tracesError"
        :empty="empty"
        @retry="reload"
      >
        <template #empty>
          <el-empty description="暂无 Trace">
            <p class="traces__empty-hint">
              当前筛选条件下没有 Trace；可调整来源、模型、工具或时间范围后重试。
            </p>
          </el-empty>
        </template>

        <div class="traces__table-wrap">
          <div
            class="traces__table"
            role="table"
            aria-label="Trace 列表"
            :aria-rowcount="store.tracesTotal"
          >
            <div class="traces__row traces__row--head" role="row">
              <span role="columnheader">状态</span>
              <span role="columnheader">名称 / Trace ID</span>
              <span role="columnheader">来源</span>
              <span role="columnheader">开始时间</span>
              <span role="columnheader">耗时</span>
              <span role="columnheader">Spans</span>
              <span role="columnheader">错误</span>
              <span role="columnheader">Token</span>
              <span role="columnheader">成本</span>
              <span role="columnheader">操作</span>
            </div>

            <div
              ref="viewport"
              class="traces__viewport"
              data-test="trace-table-viewport"
              @scroll="handleScroll"
            >
              <div
                class="traces__spacer"
                :style="{ height: `${spacerHeight}px` }"
              >
                <div
                  class="traces__rows"
                  :style="{ transform: `translateY(${offsetTop}px)` }"
                >
                  <div
                    v-for="trace in visibleRows"
                    :key="trace.trace_id"
                    class="traces__row"
                    role="row"
                    tabindex="0"
                    data-test="trace-row"
                    @click="openTrace(trace.trace_id)"
                    @keydown.enter="openTrace(trace.trace_id)"
                  >
                    <span role="cell">
                      <el-tag
                        :type="runStatusTagType(trace.status)"
                        effect="light"
                        size="small"
                      >
                        {{ runStatusLabel(trace.status) }}
                      </el-tag>
                    </span>

                    <span role="cell" class="traces__name">
                      <span class="traces__name-text">{{ traceName(trace) }}</span>
                      <span class="traces__trace-id" :title="trace.trace_id">
                        {{ trace.trace_id }}
                      </span>
                    </span>

                    <span role="cell" class="traces__muted">{{ trace.source }}</span>

                    <span role="cell" class="traces__muted">
                      {{ formatDateTime(trace.start_time) }}
                    </span>

                    <span role="cell">{{ durationText(trace.duration_ms) }}</span>

                    <span role="cell">{{ trace.total_spans }}</span>

                    <span role="cell">{{ trace.error_count }}</span>

                    <span role="cell">{{ formatTokens(trace.total_tokens) }}</span>

                    <span role="cell">{{ formatCost(trace.total_cost) }}</span>

                    <span role="cell">
                      <el-button
                        link
                        type="primary"
                        @click.stop="openTrace(trace.trace_id)"
                      >
                        查看
                      </el-button>
                    </span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div v-if="store.tracesTotal > 0" class="traces__pagination">
          <el-pagination
            :current-page="page"
            :page-size="PAGE_SIZE"
            :total="store.tracesTotal"
            layout="total, prev, pager, next"
            background
            @current-change="handlePageChange"
          />
        </div>
      </AsyncState>
    </div>
  </section>
</template>

<style scoped>
.traces {
  width: 100%;
}

.traces__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
  padding: 18px 20px;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.traces__title {
  margin: 0;
  color: #111827;
  font-size: 18px;
  font-weight: 600;
}

.traces__subtitle {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.6;
}

.traces__body {
  margin-top: 12px;
  padding: 16px;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.traces__table-wrap {
  overflow-x: auto;
}

.traces__table {
  min-width: 960px;
}

.traces__row {
  display: grid;
  grid-template-columns:
    84px minmax(190px, 1.6fr) 100px 140px 96px 64px 64px 84px 84px 64px;
  align-items: center;
  gap: 8px;
  height: 44px;
  padding: 0 12px;
  border-bottom: 1px solid #f1f5f9;
  color: #334155;
  font-size: 13px;
}

.traces__row--head {
  height: 40px;
  border-bottom: 1px solid #e5e9f2;
  color: #6b7280;
  font-size: 12px;
  font-weight: 600;
}

.traces__viewport {
  height: 480px;
  overflow-y: auto;
  overscroll-behavior: contain;
}

.traces__spacer {
  position: relative;
}

.traces__rows {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
}

.traces__row:not(.traces__row--head) {
  cursor: pointer;
}

.traces__row:not(.traces__row--head):hover,
.traces__row:not(.traces__row--head):focus-visible {
  background: #f8fafc;
  outline: none;
}

.traces__name {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}

.traces__name-text {
  overflow: hidden;
  color: #111827;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.traces__trace-id {
  overflow: hidden;
  color: #94a3b8;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.traces__muted {
  color: #64748b;
  font-size: 12px;
}

.traces__pagination {
  display: flex;
  justify-content: flex-end;
  padding-top: 16px;
}

.traces__empty-hint {
  margin: 0;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.7;
}
</style>

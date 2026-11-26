<script setup lang="ts">
import { computed, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import AsyncState from '@/components/common/AsyncState.vue'
import SpanInspector from '@/components/observability/SpanInspector.vue'
import SpanTimeline from '@/components/observability/SpanTimeline.vue'
import SpanTree from '@/components/observability/SpanTree.vue'
import { useObservabilityStore } from '@/stores/observability'
import { formatDateTime } from '@/utils/datetime'
import {
  formatCost,
  formatDurationMs,
  formatTokens,
  runStatusLabel,
  runStatusTagType,
} from '@/utils/status'

/**
 * Trace 三栏详情：左 Span 树 / 中时间线 / 右检查器。
 * 数据全部来自观测 API；加载失败用 AsyncState 呈现，不做本地兜底数据。
 */
const route = useRoute()
const router = useRouter()
const store = useObservabilityStore()

function readTraceId(): string | null {
  const raw = route.params.traceId
  const value = Array.isArray(raw) ? raw[0] : raw
  return typeof value === 'string' && value.trim() ? value : null
}

function readQuerySpan(): string {
  const raw = route.query.span
  const value = Array.isArray(raw) ? raw[0] : raw
  return typeof value === 'string' ? value : ''
}

const traceId = computed(() => readTraceId())
const invalidId = computed(() => traceId.value === null)

const detail = computed(() => store.traceDetail)
const treeEmpty = computed(
  () =>
    !store.traceDetailLoading &&
    store.traceDetailError === null &&
    store.traceTree.length === 0,
)
const traceDurationMs = computed(() => detail.value?.duration_ms ?? null)

/** Trace ID 复制失败时明确提示，不静默吞掉异常。 */
async function copyTraceId(): Promise<void> {
  const id = traceId.value
  if (!id) {
    return
  }
  try {
    if (!navigator.clipboard || typeof navigator.clipboard.writeText !== 'function') {
      throw new Error('clipboard unavailable')
    }
    await navigator.clipboard.writeText(id)
    ElMessage.success('Trace ID 已复制')
  } catch {
    ElMessage.warning('复制失败，请手动选择 Trace ID 复制')
  }
}

/**
 * 加载完成后再按 ?span= 恢复选中；刷新页面可停留在同一个 Span。
 *
 * 关键节点：
 * 1. 先加载整页数据，再校验 URL 中的 span 是否存在于本次 Trace；
 * 2. trace_id 变化时丢弃过期结果，避免把上一条 Trace 的树画出来。
 */
async function load(): Promise<void> {
  const id = traceId.value
  if (!id) {
    return
  }
  await store.loadTraceDetail(id)
  if (store.traceDetailError !== null || store.traceDetail?.trace_id !== id) {
    return
  }
  const urlSpan = readQuerySpan()
  if (urlSpan && store.traceSpans.some((span) => span.span_id === urlSpan)) {
    void store.selectTraceSpan(urlSpan)
  }
}

/**
 * 选中 Span：左侧树与时间线共用。
 *
 * 关键节点：选中态与 URL 的 `?span=` 同步，刷新或分享链接可恢复到同一个 Span；
 * 已选中同一 Span 时不重复请求详情。
 *
 * @param spanId 目标 Span 的 16 位 ID。
 */
async function selectSpan(spanId: string): Promise<void> {
  if (store.selectedSpanId !== spanId) {
    void store.selectTraceSpan(spanId)
  }
  if (readQuerySpan() !== spanId) {
    await router.replace({ query: { ...route.query, span: spanId } })
  }
}

function retrySpan(spanId: string): void {
  void store.selectTraceSpan(spanId)
}

function backToList(): void {
  void router.push({ name: 'observability-traces' })
}

watch(
  traceId,
  (id) => {
    if (id) {
      void load()
    }
  },
  { immediate: true },
)

/** null 表示未上报，不显示成 0。 */
function durationText(value: number | null | undefined): string {
  return value === null || value === undefined ? '未上报' : formatDurationMs(value)
}

function traceName(): string {
  return detail.value?.name.trim() || '未命名 Trace'
}
</script>

<template>
  <section class="trace-detail">
    <header class="trace-detail__header">
      <div class="trace-detail__heading">
        <el-button size="small" data-test="back-to-traces" @click="backToList">
          返回 Trace 列表
        </el-button>
        <div class="trace-detail__title-wrap">
          <h2 class="trace-detail__title" data-test="trace-detail-name">
            {{ traceName() }}
          </h2>
          <div class="trace-detail__id-row">
            <span class="trace-detail__id" data-test="trace-detail-id">
              {{ traceId ?? '缺少 trace_id' }}
            </span>
            <el-button
              v-if="traceId"
              link
              type="primary"
              size="small"
              data-test="copy-trace-id"
              @click="copyTraceId"
            >
              复制
            </el-button>
          </div>
        </div>
      </div>

      <div v-if="detail" class="trace-detail__summary">
        <span class="trace-detail__summary-item">
          <el-tag :type="runStatusTagType(detail.status)" effect="light" size="small">
            {{ runStatusLabel(detail.status) }}
          </el-tag>
        </span>
        <span class="trace-detail__summary-item">
          来源：<strong>{{ detail.source }}</strong>
        </span>
        <span class="trace-detail__summary-item">
          开始：{{ formatDateTime(detail.start_time) }}
        </span>
        <span class="trace-detail__summary-item">
          耗时：{{ durationText(detail.duration_ms) }}
        </span>
        <span class="trace-detail__summary-item">
          Token：{{ formatTokens(detail.total_tokens) }}
        </span>
        <span class="trace-detail__summary-item">
          成本：{{ formatCost(detail.total_cost) }}
          <span v-if="detail.total_cost === null" class="trace-detail__note">（未上报，不估算）</span>
        </span>
      </div>
    </header>

    <el-alert
      v-if="invalidId"
      type="error"
      :closable="false"
      show-icon
      title="URL 缺少有效的 trace_id。"
      data-test="trace-id-missing"
    />

    <AsyncState
      v-else
      :loading="store.traceDetailLoading"
      :error="store.traceDetailError"
      :empty="treeEmpty"
      @retry="load()"
    >
      <template #empty>
        <el-empty description="该 Trace 暂无 Span 数据">
          <p class="trace-detail__empty-hint">
            后端未上报任何 Span；页面不会生成占位节点或模拟时间线。
          </p>
        </el-empty>
      </template>

      <div class="trace-detail__grid">
        <aside class="trace-detail__left" data-test="trace-detail-left">
          <h3 class="trace-detail__pane-title">Span 树</h3>
          <SpanTree
            :nodes="store.traceTree"
            :selected-span-id="store.selectedSpanId"
            :trace-duration-ms="traceDurationMs"
            @select="selectSpan"
          />
        </aside>

        <section class="trace-detail__center" data-test="trace-detail-center">
          <SpanTimeline
            :spans="store.traceSpans"
            :selected-span-id="store.selectedSpanId"
            @select="selectSpan"
          />
        </section>

        <aside class="trace-detail__right" data-test="trace-detail-right">
          <SpanInspector
            :span="store.spanDetail"
            :events="store.traceEvents"
            :selected-span-id="store.selectedSpanId"
            :loading="store.spanDetailLoading"
            :error="store.spanDetailError"
            @retry="retrySpan"
          />
        </aside>
      </div>
    </AsyncState>
  </section>
</template>

<style scoped>
.trace-detail {
  width: 100%;
}

.trace-detail__header {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px 24px;
  margin-bottom: 12px;
  padding: 16px 20px;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.trace-detail__heading {
  display: flex;
  min-width: 0;
  align-items: flex-start;
  gap: 12px;
}

.trace-detail__title-wrap {
  min-width: 0;
}

.trace-detail__title {
  margin: 0;
  overflow: hidden;
  color: #111827;
  font-size: 18px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.trace-detail__id-row {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 2px;
}

.trace-detail__id {
  color: #94a3b8;
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 11px;
  word-break: break-all;
}

.trace-detail__summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 16px;
  color: #475569;
  font-size: 12px;
}

.trace-detail__summary-item strong {
  color: #111827;
}

.trace-detail__note {
  color: #b45309;
}

.trace-detail__grid {
  display: grid;
  /* 侧边栏占用后 1280px 视口的内容区约 1000px，固定 300+420 会裁掉右侧检查器。
     这里收窄左右栏并让中栏吃剩余空间，三栏在常见桌面宽度下都能完整显示。 */
  grid-template-columns: 240px minmax(220px, 1fr) 330px;
  align-items: start;
  gap: 12px;
  padding: 12px;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.trace-detail__pane-title {
  margin: 0 0 8px;
  color: #111827;
  font-size: 13px;
  font-weight: 600;
}

.trace-detail__left,
.trace-detail__center,
.trace-detail__right {
  min-width: 0;
}

.trace-detail__left,
.trace-detail__right {
  max-height: 720px;
  overflow: auto;
}

.trace-detail__empty-hint {
  margin: 0;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.7;
}

/* 窄屏改为上下堆叠，但左/中/右三块始终保留。 */
@media (max-width: 1200px) {
  .trace-detail__grid {
    grid-template-columns: minmax(0, 1fr);
  }

  .trace-detail__left,
  .trace-detail__right {
    max-height: none;
  }
}
</style>

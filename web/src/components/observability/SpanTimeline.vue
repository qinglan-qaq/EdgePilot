<script setup lang="ts">
import { computed, ref } from 'vue'

import type { SpanNode } from '@/api/observability'
import {
  formatDurationMs,
  isFailedSpanStatus,
  spanTypeLabel,
} from '@/utils/status'

/**
 * 自绘 Span 甘特图：不引入图表库。
 * 时间轴范围只来自真实字段：min(start_time) ~ max(end_time / start_time + duration)。
 * 缺失开始时间的 Span 不绘制条形，只在行内提示“未上报”。
 */
const props = withDefaults(
  defineProps<{
    spans: SpanNode[]
    selectedSpanId?: string | null
  }>(),
  {
    selectedSpanId: null,
  },
)

const emit = defineEmits<{
  select: [spanId: string]
}>()

const ZOOM_LEVELS = [1, 2, 4]
const zoom = ref(1)

const zoomIndex = computed(() => ZOOM_LEVELS.indexOf(zoom.value))
const canZoomIn = computed(() => zoomIndex.value < ZOOM_LEVELS.length - 1)
const canZoomOut = computed(() => zoomIndex.value > 0)

function zoomIn(): void {
  if (canZoomIn.value) {
    zoom.value = ZOOM_LEVELS[zoomIndex.value + 1]
  }
}

function zoomOut(): void {
  if (canZoomOut.value) {
    zoom.value = ZOOM_LEVELS[zoomIndex.value - 1]
  }
}

interface TimelineEntry {
  span: SpanNode
  start: number | null
  end: number | null
  /** 相对时间轴左端的百分比。 */
  left: number
  /** 相对时间轴总长的百分比。 */
  width: number
  retried: boolean
  failed: boolean
  missing: boolean
}

function parseTime(value: unknown): number | null {
  if (typeof value !== 'string' || !value.trim()) {
    return null
  }
  const parsed = new Date(value).getTime()
  return Number.isNaN(parsed) ? null : parsed
}

/** 结束时间优先取 end_time；没有时用 start_time + duration_ms，仍缺失则不猜。 */
function resolveEnd(span: SpanNode, start: number | null): number | null {
  const end = parseTime(span.end_time)
  if (end !== null) {
    return end
  }
  if (start !== null && typeof span.duration_ms === 'number' && Number.isFinite(span.duration_ms)) {
    return start + Math.max(0, span.duration_ms)
  }
  return null
}

const range = computed<{ start: number; end: number } | null>(() => {
  let start: number | null = null
  let end: number | null = null
  for (const span of props.spans) {
    const spanStart = parseTime(span.start_time)
    if (spanStart === null) {
      continue
    }
    start = start === null ? spanStart : Math.min(start, spanStart)
    const spanEnd = resolveEnd(span, spanStart)
    if (spanEnd !== null) {
      end = end === null ? spanEnd : Math.max(end, spanEnd)
    } else {
      end = end === null ? spanStart : Math.max(end, spanStart)
    }
  }
  if (start === null) {
    return null
  }
  const safeEnd = end === null || end <= start ? start + 1 : end
  return { start, end: safeEnd }
})

const entries = computed<TimelineEntry[]>(() => {
  const bounds = range.value
  if (!bounds) {
    return []
  }
  const total = bounds.end - bounds.start
  return props.spans.map((span) => {
    const start = parseTime(span.start_time)
    const end = resolveEnd(span, start)
    const missing = start === null
    const left = missing ? 0 : ((start - bounds.start) / total) * 100
    const width =
      missing || end === null ? 0 : Math.max(0, ((end - start) / total) * 100)
    return {
      span,
      start,
      end,
      left,
      width,
      retried: typeof span.attempt === 'number' && span.attempt > 1,
      failed: isFailedSpanStatus(span.status),
      missing,
    }
  })
})

/** 时间刻度：只展示相对秒数，避免把缺失时间当成 0。 */
const ticks = computed(() => {
  const bounds = range.value
  if (!bounds) {
    return []
  }
  const total = bounds.end - bounds.start
  return [0, 25, 50, 75, 100].map((percent) => ({
    percent,
    label: `${formatDurationMs((total * percent) / 100)}`,
  }))
})
</script>

<template>
  <div class="span-timeline" data-test="span-timeline">
    <div class="span-timeline__toolbar">
      <span class="span-timeline__title">时间线</span>
      <div class="span-timeline__zoom">
        <el-button
          size="small"
          :disabled="!canZoomOut"
          data-test="timeline-zoom-out"
          @click="zoomOut"
        >
          缩小
        </el-button>
        <span class="span-timeline__zoom-value" data-test="timeline-zoom">{{ zoom }}x</span>
        <el-button
          size="small"
          :disabled="!canZoomIn"
          data-test="timeline-zoom-in"
          @click="zoomIn"
        >
          放大
        </el-button>
      </div>
    </div>

    <p v-if="entries.length === 0" class="span-timeline__empty" data-test="span-timeline-empty">
      暂无 Span 数据
    </p>

    <template v-else-if="!range">
      <p class="span-timeline__empty" data-test="span-timeline-empty">
        所有 Span 均未上报开始时间，无法绘制时间线。
      </p>
    </template>

    <div v-else class="span-timeline__scroll">
      <div
        class="span-timeline__canvas"
        :style="{ width: `${zoom * 100}%` }"
      >
        <div class="span-timeline__axis">
          <span class="span-timeline__axis-label">Span</span>
          <div class="span-timeline__axis-track">
            <span
              v-for="tick in ticks"
              :key="tick.percent"
              class="span-timeline__tick"
              :style="{ left: `${tick.percent}%` }"
            >
              {{ tick.label }}
            </span>
          </div>
        </div>

        <div
          v-for="entry in entries"
          :key="entry.span.span_id"
          class="span-timeline__row"
          :class="{ 'span-timeline__row--selected': entry.span.span_id === selectedSpanId }"
        >
          <button
            type="button"
            class="span-timeline__label"
            :title="entry.span.name"
            @click="emit('select', entry.span.span_id)"
          >
            <el-tag size="small" type="info" effect="plain">
              {{ spanTypeLabel(entry.span.span_type) }}
            </el-tag>
            <span class="span-timeline__name">{{ entry.span.name }}</span>
          </button>

          <div class="span-timeline__track">
            <span v-if="entry.missing" class="span-timeline__missing">开始时间未上报</span>
            <button
              v-else
              type="button"
              class="span-timeline__bar"
              :class="{
                'span-timeline__bar--selected': entry.span.span_id === selectedSpanId,
                'span-timeline__bar--retry': entry.retried,
                'span-timeline__bar--failed': entry.failed,
              }"
              :style="{ left: `${entry.left}%`, width: `${entry.width}%` }"
              :title="`${entry.span.name} · ${formatDurationMs(entry.span.duration_ms)}`"
              :data-test="`timeline-bar-${entry.span.span_id}`"
              @click="emit('select', entry.span.span_id)"
            >
              <span v-if="entry.retried" class="span-timeline__retry-mark">重试</span>
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.span-timeline {
  min-width: 0;
  border: 1px solid #eef1f6;
  border-radius: 10px;
  background: #ffffff;
}

.span-timeline__toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border-bottom: 1px solid #eef1f6;
}

.span-timeline__title {
  color: #111827;
  font-size: 13px;
  font-weight: 600;
}

.span-timeline__zoom {
  display: flex;
  align-items: center;
  gap: 8px;
}

.span-timeline__zoom-value {
  color: #64748b;
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.span-timeline__scroll {
  overflow-x: auto;
  padding: 8px 12px 14px;
}

.span-timeline__canvas {
  min-width: 100%;
}

.span-timeline__axis,
.span-timeline__row {
  display: grid;
  /* 中栏在 1280px 视口下约 350px，收窄名称列后时间轴仍保留可读宽度，
     更长的名称由 ellipsis + title 兜底。 */
  grid-template-columns: 140px minmax(160px, 1fr);
  align-items: center;
  gap: 8px;
}

.span-timeline__axis {
  height: 26px;
}

.span-timeline__axis-label {
  color: #94a3b8;
  font-size: 11px;
}

.span-timeline__axis-track {
  position: relative;
  height: 100%;
  border-bottom: 1px solid #eef1f6;
}

.span-timeline__tick {
  position: absolute;
  top: 0;
  transform: translateX(-50%);
  color: #94a3b8;
  font-size: 10px;
  white-space: nowrap;
}

.span-timeline__row {
  height: 32px;
  border-radius: 6px;
}

.span-timeline__row--selected {
  background: #eff6ff;
}

.span-timeline__label {
  display: flex;
  min-width: 0;
  align-items: center;
  gap: 6px;
  padding: 0;
  border: 0;
  background: transparent;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.span-timeline__name {
  overflow: hidden;
  color: #334155;
  font-size: 12px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.span-timeline__track {
  position: relative;
  height: 20px;
}

.span-timeline__bar {
  position: absolute;
  top: 4px;
  height: 12px;
  min-width: 3px;
  padding: 0;
  border: 1px solid #93c5fd;
  border-radius: 4px;
  background: #bfdbfe;
  cursor: pointer;
}

.span-timeline__bar:hover {
  background: #93c5fd;
}

.span-timeline__bar--selected {
  border-color: #1d4ed8;
  background: #3b82f6;
  box-shadow: 0 0 0 2px rgba(29, 78, 216, 0.18);
}

.span-timeline__bar--failed {
  border-color: #ef4444;
  background: #fecaca;
}

.span-timeline__bar--failed.span-timeline__bar--selected {
  background: #ef4444;
}

/* attempt > 1 用虚线边框标记重试，而不是伪造额外条形。 */
.span-timeline__bar--retry {
  border-style: dashed;
  border-width: 2px;
}

.span-timeline__retry-mark {
  position: absolute;
  top: -14px;
  left: 0;
  color: #b45309;
  font-size: 10px;
  white-space: nowrap;
}

.span-timeline__missing {
  position: absolute;
  top: 0;
  color: #94a3b8;
  font-size: 11px;
}

.span-timeline__empty {
  margin: 0;
  padding: 32px 16px;
  color: #94a3b8;
  font-size: 13px;
  text-align: center;
}
</style>

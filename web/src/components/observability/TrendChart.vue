<script setup lang="ts">
import { computed } from 'vue'

import type { TrendPoint, TrendSeries } from './chart-types'

const props = withDefaults(
  defineProps<{
    points: TrendPoint[]
    series: TrendSeries[]
    kind?: 'line' | 'bar'
    /** 可选的数值格式化；null 会由组件直接展示“未上报” */
    valueFormatter?: (value: number | null) => string
  }>(),
  {
    kind: 'line',
    valueFormatter: undefined,
  },
)

const WIDTH = 640
const HEIGHT = 240
const PADDING = { top: 16, right: 16, bottom: 28, left: 56 }
const PLOT_WIDTH = WIDTH - PADDING.left - PADDING.right
const PLOT_HEIGHT = HEIGHT - PADDING.top - PADDING.bottom

const DEFAULT_COLORS = ['#2563eb', '#16a34a', '#f97316', '#9333ea', '#0891b2', '#dc2626']

const hasPoints = computed(() => props.points.length > 0)

const formatValue = computed<(value: number | null) => string>(() => {
  const formatter = props.valueFormatter
  return formatter ?? ((value: number | null) => (value === null ? '未上报' : String(value)))
})

const maxValue = computed(() => {
  let max = 0
  for (const point of props.points) {
    for (const item of props.series) {
      const value = point.values[item.key]
      if (typeof value === 'number' && Number.isFinite(value)) {
        max = Math.max(max, value)
      }
    }
  }
  return max > 0 ? max : 1
})

const ariaLabel = computed(() => {
  const labels = props.series.map((item) => item.label).join('、')
  return labels ? `${labels} 趋势图` : '趋势图'
})

function colorOf(index: number): string {
  return props.series[index]?.color ?? DEFAULT_COLORS[index % DEFAULT_COLORS.length] ?? '#2563eb'
}

function xFor(index: number): number {
  if (props.points.length <= 1) {
    return PADDING.left + PLOT_WIDTH / 2
  }
  // 柱状图按等宽分组居中，折线图按首尾对齐。
  if (props.kind === 'bar') {
    return PADDING.left + ((index + 0.5) / props.points.length) * PLOT_WIDTH
  }
  return PADDING.left + (index / (props.points.length - 1)) * PLOT_WIDTH
}

function yFor(value: number): number {
  const ratio = Math.min(1, Math.max(0, value / maxValue.value))
  return PADDING.top + (1 - ratio) * PLOT_HEIGHT
}

interface LineSegment {
  id: string
  path: string
  color: string
}

const lineSegments = computed<LineSegment[]>(() => {
  const segments: LineSegment[] = []
  props.series.forEach((item, seriesIndex) => {
    let commands: string[] = []
    let segmentIndex = 0
    const flush = (): void => {
      if (commands.length > 0) {
        segments.push({
          id: `${item.key}-${segmentIndex}`,
          path: commands.join(' '),
          color: colorOf(seriesIndex),
        })
        segmentIndex += 1
        commands = []
      }
    }

    props.points.forEach((point, pointIndex) => {
      const value = point.values[item.key]
      if (typeof value === 'number' && Number.isFinite(value)) {
        const command = commands.length === 0 ? 'M' : 'L'
        commands.push(`${command}${xFor(pointIndex).toFixed(2)},${yFor(value).toFixed(2)}`)
      } else {
        // null 表示未上报：断开折线，不做 0 填补。
        flush()
      }
    })
    flush()
  })
  return segments
})

interface DotMark {
  id: string
  cx: number
  cy: number
  color: string
  title: string
}

const dots = computed<DotMark[]>(() => {
  const result: DotMark[] = []
  props.series.forEach((item, seriesIndex) => {
    props.points.forEach((point, pointIndex) => {
      const value = point.values[item.key]
      if (typeof value === 'number' && Number.isFinite(value)) {
        result.push({
          id: `${item.key}-${pointIndex}`,
          cx: xFor(pointIndex),
          cy: yFor(value),
          color: colorOf(seriesIndex),
          title: `${point.label} · ${item.label}: ${formatValue.value(value)}`,
        })
      }
    })
  })
  return result
})

interface BarMark {
  id: string
  x: number
  y: number
  width: number
  height: number
  color: string
  title: string
}

const bars = computed<BarMark[]>(() => {
  const result: BarMark[] = []
  const pointCount = props.points.length
  if (pointCount === 0) {
    return result
  }
  const groupWidth = PLOT_WIDTH / pointCount
  const groupPadding = groupWidth * 0.2
  const seriesCount = Math.max(1, props.series.length)
  const slotWidth = (groupWidth - groupPadding) / seriesCount
  const barWidth = Math.max(1, slotWidth * 0.8)
  const baseline = PADDING.top + PLOT_HEIGHT

  props.series.forEach((item, seriesIndex) => {
    props.points.forEach((point, pointIndex) => {
      const value = point.values[item.key]
      if (typeof value === 'number' && Number.isFinite(value)) {
        const y = yFor(value)
        result.push({
          id: `${item.key}-${pointIndex}`,
          x: PADDING.left + pointIndex * groupWidth + groupPadding / 2 + seriesIndex * slotWidth,
          y,
          width: barWidth,
          height: Math.max(0, baseline - y),
          color: colorOf(seriesIndex),
          title: `${point.label} · ${item.label}: ${formatValue.value(value)}`,
        })
      }
    })
  })
  return result
})

interface AxisLabel {
  x: number
  text: string
}

const xLabels = computed<AxisLabel[]>(() => {
  const count = props.points.length
  if (count === 0) {
    return []
  }
  const step = Math.max(1, Math.ceil(count / 8))
  const indexes: number[] = []
  for (let index = 0; index < count; index += step) {
    indexes.push(index)
  }
  const lastIndex = count - 1
  if (indexes[indexes.length - 1] !== lastIndex) {
    if (indexes.length < 8) {
      indexes.push(lastIndex)
    } else {
      indexes[indexes.length - 1] = lastIndex
    }
  }
  return indexes.map((index) => ({
    x: xFor(index),
    text: props.points[index]?.label ?? '',
  })).filter((item) => item.text)
})

const gridLines = computed(() =>
  [0, 0.5, 1].map((ratio) => ({
    y: PADDING.top + ratio * PLOT_HEIGHT,
  })),
)

const legend = computed(() =>
  props.series.map((item, index) => ({
    key: item.key,
    label: item.label,
    color: colorOf(index),
  })),
)
</script>

<template>
  <div class="trend-chart" data-test="trend-chart">
    <p v-if="!hasPoints" class="trend-chart__empty">暂无数据</p>

    <template v-else>
      <ul class="trend-chart__legend" aria-hidden="true">
        <li v-for="item in legend" :key="item.key" class="trend-chart__legend-item">
          <span class="trend-chart__swatch" :style="{ background: item.color }" />
          <span>{{ item.label }}</span>
        </li>
      </ul>

      <svg
        class="trend-chart__svg"
        :viewBox="`0 0 ${WIDTH} ${HEIGHT}`"
        preserveAspectRatio="xMidYMid meet"
        role="img"
        :aria-label="ariaLabel"
      >
        <title>{{ ariaLabel }}</title>

        <g aria-hidden="true">
          <line
            v-for="(line, index) in gridLines"
            :key="index"
            :x1="PADDING.left"
            :x2="WIDTH - PADDING.right"
            :y1="line.y"
            :y2="line.y"
            class="trend-chart__grid-line"
          />
        </g>

        <g v-if="kind === 'bar'">
          <rect
            v-for="bar in bars"
            :key="bar.id"
            :x="bar.x"
            :y="bar.y"
            :width="bar.width"
            :height="bar.height"
            :fill="bar.color"
            rx="2"
          >
            <title>{{ bar.title }}</title>
          </rect>
        </g>

        <g v-else>
          <path
            v-for="segment in lineSegments"
            :key="segment.id"
            :d="segment.path"
            fill="none"
            :stroke="segment.color"
            stroke-width="2"
            stroke-linecap="round"
            stroke-linejoin="round"
          />
          <circle
            v-for="dot in dots"
            :key="dot.id"
            :cx="dot.cx"
            :cy="dot.cy"
            r="3"
            :fill="dot.color"
          >
            <title>{{ dot.title }}</title>
          </circle>
        </g>

        <g aria-hidden="true">
          <text
            v-for="(label, index) in xLabels"
            :key="index"
            :x="label.x"
            :y="HEIGHT - 8"
            class="trend-chart__axis-label"
            text-anchor="middle"
          >
            {{ label.text }}
          </text>
        </g>
      </svg>
    </template>
  </div>
</template>

<style scoped>
.trend-chart {
  width: 100%;
}

.trend-chart__empty {
  margin: 0;
  padding: 32px 0;
  color: #94a3b8;
  font-size: 13px;
  text-align: center;
}

.trend-chart__legend {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin: 0 0 8px;
  padding: 0;
  list-style: none;
}

.trend-chart__legend-item {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  color: #475569;
  font-size: 12px;
}

.trend-chart__swatch {
  display: inline-block;
  width: 10px;
  height: 10px;
  border-radius: 2px;
}

.trend-chart__svg {
  display: block;
  width: 100%;
  height: auto;
  overflow: visible;
}

.trend-chart__grid-line {
  stroke: #eef2f7;
  stroke-width: 1;
}

.trend-chart__axis-label {
  fill: #94a3b8;
  font-size: 10px;
}
</style>

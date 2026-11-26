<script setup lang="ts">
import { computed, ref } from 'vue'

import type { VisionFrame } from '@/api/vision'
import { filterDetections } from '@/utils/vision'

/**
 * 当前采样帧的目标面板。
 *
 * 关键节点：
 * 1. 只展示播放器当前命中的 artifact 帧，不自行抽帧或补帧；
 * 2. 置信度阈值是纯前端过滤，不重新请求后端；
 * 3. 目标按置信度降序展示，点击行通知父组件选中并跳转到该帧时间点。
 */
const props = withDefaults(
  defineProps<{
    frame?: VisionFrame | null
    selectedId?: string | null
    minConfidence?: number
  }>(),
  {
    frame: null,
    selectedId: null,
    minConfidence: 0.25,
  },
)

const emit = defineEmits<{
  (event: 'select', detectionId: string): void
  (event: 'seek', timestampMs: number): void
}>()

/** 前端阈值：默认与后端任务 conf_threshold 常用值一致。 */
const threshold = ref(props.minConfidence)

const detections = computed(() => {
  if (!props.frame) {
    return []
  }
  return [...filterDetections(props.frame, threshold.value)].sort(
    (left, right) => right.confidence - left.confidence,
  )
})

const frameTime = computed(() => {
  if (!props.frame) {
    return '—'
  }
  return `${(props.frame.timestamp_ms / 1000).toFixed(2)}s`
})

function confidenceText(value: number): string {
  return `${(value * 100).toFixed(1)}%`
}

function bboxText(bbox: { x1: number; y1: number; x2: number; y2: number }): string {
  return `[${Math.round(bbox.x1)}, ${Math.round(bbox.y1)}, ${Math.round(bbox.x2)}, ${Math.round(bbox.y2)}]`
}

/** 点击目标：先选中，再跳回该帧时间点，让播放器高亮对应框。 */
function handleSelect(detectionId: string): void {
  emit('select', detectionId)
  if (props.frame) {
    emit('seek', props.frame.timestamp_ms)
  }
}
</script>

<template>
  <section class="detection-panel" data-test="detection-panel">
    <header class="detection-panel__header">
      <h3 class="detection-panel__title">当前帧目标</h3>
      <span v-if="frame" class="detection-panel__meta">
        帧 {{ frame.frame_id }} · {{ frameTime }} · 推理
        {{ frame.inference_ms ?? '—' }} ms
      </span>
      <span v-else class="detection-panel__meta">暂无当前帧</span>
    </header>

    <div v-if="frame" class="detection-panel__filter">
      <span class="detection-panel__filter-label">最低置信度</span>
      <el-slider
        v-model="threshold"
        class="detection-panel__slider"
        :min="0"
        :max="1"
        :step="0.05"
        size="small"
      />
      <span class="detection-panel__threshold">{{ threshold.toFixed(2) }}</span>
      <span class="detection-panel__count">
        {{ detections.length }} / {{ frame.detections.length }} 个目标
      </span>
    </div>

    <ul v-if="detections.length > 0" class="detection-panel__list">
      <li
        v-for="detection in detections"
        :key="detection.detection_id"
        class="detection-panel__item"
        :class="{ 'is-selected': detection.detection_id === selectedId }"
        role="button"
        tabindex="0"
        data-test="detection-item"
        @click="handleSelect(detection.detection_id)"
        @keydown.enter.prevent="handleSelect(detection.detection_id)"
        @keydown.space.prevent="handleSelect(detection.detection_id)"
      >
        <span class="detection-panel__class">{{ detection.class_name }}</span>
        <span class="detection-panel__confidence">
          {{ confidenceText(detection.confidence) }}
        </span>
        <span class="detection-panel__bbox">{{ bboxText(detection.bbox) }}</span>
      </li>
    </ul>

    <p v-else-if="frame" class="detection-panel__empty">
      当前帧没有满足置信度的目标
    </p>
    <p v-else class="detection-panel__empty">播放视频后显示当前帧目标</p>
  </section>
</template>

<style scoped>
.detection-panel {
  margin-top: 12px;
  padding: 12px 14px;
  border: 1px solid #e2e8f0;
  border-radius: 10px;
  background: #f8fafc;
}

.detection-panel__header {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.detection-panel__title {
  margin: 0;
  color: #111827;
  font-size: 14px;
  font-weight: 600;
}

.detection-panel__meta {
  color: #64748b;
  font-size: 12px;
}

.detection-panel__filter {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-top: 8px;
  color: #64748b;
  font-size: 12px;
}

.detection-panel__filter-label,
.detection-panel__count {
  white-space: nowrap;
}

.detection-panel__slider {
  width: 160px;
  margin: 0 4px;
}

.detection-panel__threshold {
  color: #334155;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.detection-panel__list {
  margin: 10px 0 0;
  padding: 0;
  list-style: none;
  max-height: 220px;
  overflow-y: auto;
}

.detection-panel__item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 6px 8px;
  border-radius: 6px;
  color: #334155;
  font-size: 13px;
  cursor: pointer;
}

.detection-panel__item:hover {
  background: #eef2f7;
}

.detection-panel__item.is-selected {
  background: #fee2e2;
  color: #b91c1c;
}

.detection-panel__class {
  min-width: 70px;
  font-weight: 600;
}

.detection-panel__confidence {
  min-width: 56px;
  color: #2563eb;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
}

.detection-panel__bbox {
  color: #94a3b8;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}

.detection-panel__empty {
  margin: 10px 0 0;
  color: #94a3b8;
  font-size: 13px;
}
</style>

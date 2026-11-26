<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { VisionFrame } from '@/api/vision'
import { findFrameAt, scaleBBox } from '@/utils/vision'

/**
 * 视频播放器 + Canvas 检测框覆盖层。
 *
 * 关键节点：
 * 1. 帧数据只来自结果接口，播放器不做任何抽帧或伪造；
 * 2. Canvas 与 `<video>` 同尺寸，检测框按 `object-fit: contain` 换算黑边偏移；
 * 3. 绘制统一走 requestAnimationFrame，多个播放事件会合并成一帧绘制；
 * 4. 视频加载失败后只清空画布并展示错误，不再绘制任何框；
 * 5. 当前帧通过 `frame-change` 通知父组件，父组件可调用暴露的 `seek` 回跳。
 */
const props = withDefaults(
  defineProps<{
    src: string
    frames?: readonly VisionFrame[]
    videoWidth?: number | null
    videoHeight?: number | null
    selectedId?: string | null
  }>(),
  {
    frames: () => [],
    videoWidth: null,
    videoHeight: null,
    selectedId: null,
  },
)

const emit = defineEmits<{
  (event: 'frame-change', frame: VisionFrame | null): void
}>()

const videoRef = ref<HTMLVideoElement | null>(null)
const canvasRef = ref<HTMLCanvasElement | null>(null)
const failed = ref(false)

let rafId: number | null = null
/** 上一次通知父组件的帧键；同一帧重复绘制不重复 emit。 */
let lastFrameKey: string | null | undefined

function frameKey(frame: VisionFrame | undefined): string | null {
  if (!frame) {
    return null
  }
  return `${frame.frame_id}:${frame.timestamp_ms}:${frame.inference_ms ?? ''}`
}

/** 当前帧变化时通知父组件（含 null），用于目标面板与选中态联动。 */
function emitFrameChange(frame: VisionFrame | undefined): void {
  const key = frameKey(frame)
  if (key === lastFrameKey) {
    return
  }
  lastFrameKey = key
  emit('frame-change', frame ?? null)
}

/** 把当前帧按画布尺寸与 letterbox 偏移画到 Canvas 上。 */
function paint(): void {
  const canvas = canvasRef.value
  const video = videoRef.value
  if (!canvas || !video) {
    return
  }

  const ctx = canvas.getContext('2d')
  if (!ctx) {
    return
  }

  const width = video.clientWidth
  const height = video.clientHeight
  canvas.width = width
  canvas.height = height
  ctx.clearRect(0, 0, width, height)

  const frame = failed.value
    ? undefined
    : findFrameAt(props.frames, video.currentTime * 1000)
  emitFrameChange(frame)

  // 视频不可用时只清空画布，绝不画出无坐标依据的框。
  if (failed.value) {
    return
  }

  const detections = frame?.detections ?? []
  if (detections.length === 0) {
    return
  }

  // 元数据缺失时退回 video 元素自身的固有尺寸，至少保证除零安全。
  const videoWidth = props.videoWidth || video.videoWidth || 1
  const videoHeight = props.videoHeight || video.videoHeight || 1

  for (const detection of detections) {
    const box = scaleBBox(detection.bbox, videoWidth, videoHeight, width, height)
    const boxWidth = box.x2 - box.x1
    const boxHeight = box.y2 - box.y1
    const selected = detection.detection_id === props.selectedId
    const label = `${detection.class_name} ${(detection.confidence * 100).toFixed(1)}%`

    ctx.strokeStyle = selected ? '#f56c6c' : '#409eff'
    ctx.lineWidth = selected ? 3 : 2
    ctx.strokeRect(box.x1, box.y1, boxWidth, boxHeight)

    ctx.fillStyle = 'rgba(0,0,0,.65)'
    ctx.fillRect(box.x1, Math.max(0, box.y1 - 18), 110, 18)
    ctx.fillStyle = '#fff'
    ctx.fillText(label, box.x1 + 4, Math.max(12, box.y1 - 4))
  }
}

/** 合并同一帧内的多次触发，避免 timeupdate 高频事件重复绘制。 */
function scheduleDraw(): void {
  if (rafId !== null) {
    return
  }
  rafId = requestAnimationFrame(() => {
    rafId = null
    paint()
  })
}

function handleVideoError(): void {
  failed.value = true
  scheduleDraw()
}

/**
 * 跳转到指定秒数；父组件通过目标面板点击调用。
 *
 * 视频元数据未就绪时不报错，交由浏览器在可 seek 时处理。
 */
function seek(seconds: number): void {
  const video = videoRef.value
  if (!video || !Number.isFinite(seconds)) {
    return
  }
  const target = Math.max(0, seconds)
  const duration = video.duration
  video.currentTime =
    Number.isFinite(duration) && duration > 0
      ? Math.min(target, duration)
      : target
  scheduleDraw()
}

defineExpose({ seek })

watch(
  () => [props.frames, props.selectedId] as const,
  () => {
    scheduleDraw()
  },
)

onMounted(() => {
  window.addEventListener('resize', scheduleDraw)
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', scheduleDraw)
  if (rafId !== null) {
    cancelAnimationFrame(rafId)
    rafId = null
  }
})
</script>

<template>
  <section class="vision-player" data-test="vision-player">
    <div class="vision-player__stage">
      <video
        ref="videoRef"
        class="vision-player__video"
        :src="src"
        controls
        preload="metadata"
        @loadedmetadata="scheduleDraw"
        @timeupdate="scheduleDraw"
        @seeked="scheduleDraw"
        @error="handleVideoError"
      ></video>
      <canvas ref="canvasRef" class="vision-player__canvas"></canvas>
    </div>

    <p v-if="failed" class="vision-player__error" role="alert">
      视频加载失败，无法播放检测结果
    </p>
  </section>
</template>

<style scoped>
.vision-player {
  width: 100%;
}

.vision-player__stage {
  position: relative;
  width: 100%;
  overflow: hidden;
  border-radius: 10px;
  background: #0f172a;
}

.vision-player__video {
  display: block;
  width: 100%;
  max-height: 480px;
  object-fit: contain;
}

.vision-player__canvas {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
}

.vision-player__error {
  margin: 8px 0 0;
  color: #dc2626;
  font-size: 13px;
}
</style>

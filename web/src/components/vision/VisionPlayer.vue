<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

import type { VisionFrame } from '@/api/vision'
import { findFrameAt, fitVideoRect } from '@/utils/vision'

/**
 * 视频播放器 + Canvas 检测框覆盖层。
 *
 * 关键节点：
 * 1. 帧数据只来自结果接口，播放器不做任何抽帧或伪造；
 * 2. Canvas 与 `<video>` 同尺寸，检测框按 `object-fit: contain` 换算黑边偏移；
 * 3. 绘制统一走 requestAnimationFrame，多个播放事件会合并成一帧绘制；
 * 4. 视频加载失败后只清空画布并展示错误，不再绘制任何框。
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

const videoRef = ref<HTMLVideoElement | null>(null)
const canvasRef = ref<HTMLCanvasElement | null>(null)
const failed = ref(false)

let rafId: number | null = null

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

  // 视频不可用时只清空画布，绝不画出无坐标依据的框。
  if (failed.value) {
    return
  }

  const frame = findFrameAt(props.frames, video.currentTime * 1000)
  const detections = frame?.detections ?? []
  if (detections.length === 0) {
    return
  }

  // 元数据缺失时退回 video 元素自身的固有尺寸，至少保证除零安全。
  const videoWidth = props.videoWidth || video.videoWidth || 1
  const videoHeight = props.videoHeight || video.videoHeight || 1
  const { scale, offsetX, offsetY } = fitVideoRect(
    videoWidth,
    videoHeight,
    width,
    height,
  )

  for (const detection of detections) {
    const x = offsetX + detection.bbox.x1 * scale
    const y = offsetY + detection.bbox.y1 * scale
    const boxWidth = (detection.bbox.x2 - detection.bbox.x1) * scale
    const boxHeight = (detection.bbox.y2 - detection.bbox.y1) * scale
    const selected = detection.detection_id === props.selectedId
    const label = `${detection.class_name} ${(detection.confidence * 100).toFixed(1)}%`

    ctx.strokeStyle = selected ? '#f56c6c' : '#409eff'
    ctx.lineWidth = selected ? 3 : 2
    ctx.strokeRect(x, y, boxWidth, boxHeight)

    ctx.fillStyle = 'rgba(0,0,0,.65)'
    ctx.fillRect(x, Math.max(0, y - 18), 110, 18)
    ctx.fillStyle = '#fff'
    ctx.fillText(label, x + 4, Math.max(12, y - 4))
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

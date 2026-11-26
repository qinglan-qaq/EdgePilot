/**
 * Vision 播放器的纯几何/采样辅助函数。
 *
 * 帧级检测来自后端 artifact，坐标为视频像素坐标系；画布尺寸与视频显示尺寸
 * 不一致（`object-fit: contain` 会产生黑边），因此所有绘制都要经过
 * `fitVideoRect` / `scaleBBox` 换算。
 */

/** 只依赖 timestamp 的帧结构，方便测试与 artifact 原始数据复用。 */
export interface TimedFrame {
  timestamp_ms: number
}

export interface VideoRect {
  scale: number
  offsetX: number
  offsetY: number
}

export interface PixelBBox {
  x1: number
  y1: number
  x2: number
  y2: number
}

/**
 * 取播放时间对应的采样帧：最后一个 `timestamp_ms <= timestampMs` 的帧。
 *
 * 关键节点：
 * 1. 空数组返回 undefined，绝不伪造一帧空检测；
 * 2. 播放时间早于第一帧时返回第一帧（首帧之前保持首帧）；
 * 3. 晚于最后一帧时返回最后一帧，避免末尾黑帧丢框。
 *
 * @param frames 按 timestamp_ms 升序排列的采样帧。
 * @param timestampMs 播放器的当前时间（毫秒）。
 * @returns 命中的帧，或空数组时的 undefined。
 */
export function findFrameAt<T extends TimedFrame>(
  frames: readonly T[] | null | undefined,
  timestampMs: number,
): T | undefined {
  if (!frames || frames.length === 0) {
    return undefined
  }
  if (timestampMs < frames[0].timestamp_ms) {
    return frames[0]
  }

  let low = 0
  let high = frames.length - 1
  let found = frames[0]
  while (low <= high) {
    const mid = (low + high) >> 1
    if (frames[mid].timestamp_ms <= timestampMs) {
      found = frames[mid]
      low = mid + 1
    } else {
      high = mid - 1
    }
  }
  return found
}

/**
 * 计算 `object-fit: contain` 下的缩放与黑边偏移。
 *
 * @param videoW 视频原始宽（像素）。
 * @param videoH 视频原始高（像素）。
 * @param containerW 播放容器宽（像素）。
 * @param containerH 播放容器高（像素）。
 * @returns 缩放比例与居中后的左右/上下偏移；尺寸非法时返回零缩放。
 */
export function fitVideoRect(
  videoW: number,
  videoH: number,
  containerW: number,
  containerH: number,
): VideoRect {
  if (videoW <= 0 || videoH <= 0 || containerW <= 0 || containerH <= 0) {
    return { scale: 0, offsetX: 0, offsetY: 0 }
  }

  const scale = Math.min(containerW / videoW, containerH / videoH)
  return {
    scale,
    offsetX: (containerW - videoW * scale) / 2,
    offsetY: (containerH - videoH * scale) / 2,
  }
}

/**
 * 把视频坐标系的检测框换算到画布坐标（含 letterbox 偏移）。
 *
 * @param bbox 视频像素坐标系中的检测框。
 * @param videoW 视频原始宽。
 * @param videoH 视频原始高。
 * @param containerW 画布/播放容器宽。
 * @param containerH 画布/播放容器高。
 * @returns 画布坐标系中的检测框。
 */
export function scaleBBox(
  bbox: PixelBBox,
  videoW: number,
  videoH: number,
  containerW: number,
  containerH: number,
): PixelBBox {
  const { scale, offsetX, offsetY } = fitVideoRect(
    videoW,
    videoH,
    containerW,
    containerH,
  )
  return {
    x1: offsetX + bbox.x1 * scale,
    y1: offsetY + bbox.y1 * scale,
    x2: offsetX + bbox.x2 * scale,
    y2: offsetY + bbox.y2 * scale,
  }
}

/** 检测目标的最小结构：置信度与可选类别名。 */
export interface FilterableDetection {
  confidence: number
  class_name: string
}

/**
 * 按最低置信度与类别过滤当前帧检测。
 *
 * 关键节点：阈值本身保留（`>=`），顺序与 artifact 一致，不排序、不补假目标。
 *
 * @param frame 当前采样帧；为空时返回空数组。
 * @param minConfidence 最低置信度（含）。
 * @param className 可选类别名，精确匹配。
 * @returns 过滤后的原顺序检测数组。
 */
export function filterDetections<T extends FilterableDetection>(
  frame: { detections?: readonly T[] | null } | null | undefined,
  minConfidence: number,
  className?: string,
): T[] {
  const detections = frame?.detections ?? []
  return detections.filter(
    (detection) =>
      detection.confidence >= minConfidence &&
      (className === undefined || detection.class_name === className),
  )
}

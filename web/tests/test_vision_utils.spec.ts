import { describe, expect, it } from 'vitest'

import {
  filterDetections,
  findFrameAt,
  fitVideoRect,
  scaleBBox,
} from '../src/utils/vision'

/**
 * 本地夹具类型只描述本次测试用到的字段：
 * 帧结构来自 vision-service 结果 artifact，顺序固定为 timestamp 升序。
 */
interface TestBBox {
  x1: number
  y1: number
  x2: number
  y2: number
}

interface TestDetection {
  detection_id: string
  class_id: number
  class_name: string
  confidence: number
  bbox: TestBBox
}

interface TestFrame {
  frame_id: number
  timestamp_ms: number
  inference_ms: number
  detections: TestDetection[]
}

function makeDetection(overrides: Partial<TestDetection> = {}): TestDetection {
  return {
    detection_id: '0-0',
    class_id: 0,
    class_name: 'person',
    confidence: 0.9,
    bbox: { x1: 10, y1: 10, x2: 50, y2: 90 },
    ...overrides,
  }
}

function makeFrame(overrides: Partial<TestFrame> = {}): TestFrame {
  return {
    frame_id: 0,
    timestamp_ms: 0,
    inference_ms: 42,
    detections: [],
    ...overrides,
  }
}

describe('findFrameAt', () => {
  it('在下一采样帧到来前保持当前帧', () => {
    const frames = [
      makeFrame({ frame_id: 0, timestamp_ms: 0 }),
      makeFrame({ frame_id: 5, timestamp_ms: 200 }),
    ]

    expect(findFrameAt(frames, 0)?.frame_id).toBe(0)
    expect(findFrameAt(frames, 199)?.frame_id).toBe(0)
    expect(findFrameAt(frames, 200)?.frame_id).toBe(5)
  })

  it('播放时间早于第一帧时返回第一帧，晚于最后一帧时返回最后一帧', () => {
    const frames = [
      makeFrame({ frame_id: 10, timestamp_ms: 500 }),
      makeFrame({ frame_id: 15, timestamp_ms: 800 }),
    ]

    expect(findFrameAt(frames, 0)?.frame_id).toBe(10)
    expect(findFrameAt(frames, 499)?.frame_id).toBe(10)
    expect(findFrameAt(frames, 800)?.frame_id).toBe(15)
    expect(findFrameAt(frames, 10_000)?.frame_id).toBe(15)
  })

  it('空帧数组没有可返回的帧，不伪造第一帧', () => {
    expect(findFrameAt([], 0)).toBeFalsy()
    expect(findFrameAt([], 1000)).toBeFalsy()
  })
})

describe('fitVideoRect', () => {
  it('横向黑边：100x100 视频放进 200x100 容器，左右各 50px', () => {
    expect(fitVideoRect(100, 100, 200, 100)).toEqual({
      scale: 1,
      offsetX: 50,
      offsetY: 0,
    })
  })

  it('纵向黑边：640x360 视频放进 640x480 容器，上下各 60px', () => {
    expect(fitVideoRect(640, 360, 640, 480)).toEqual({
      scale: 1,
      offsetX: 0,
      offsetY: 60,
    })
  })

  it('宽视频放进窄容器：640x480 在 640x360 内按 0.75 缩放并左右留 80px', () => {
    // 4:3 源（640x480）放进 16:9 容器（640x360）时，contain 后实际显示 480x360，
    // 黑边在左右各 80px；这里按 object-fit: contain 的数学语义断言。
    expect(fitVideoRect(640, 480, 640, 360)).toEqual({
      scale: 0.75,
      offsetX: 80,
      offsetY: 0,
    })
  })

  it('显示区域与视频比例一致时不产生偏移', () => {
    expect(fitVideoRect(1280, 720, 640, 360)).toEqual({
      scale: 0.5,
      offsetX: 0,
      offsetY: 0,
    })
  })
})

describe('scaleBBox', () => {
  it('横向 letterbox 时叠加左侧黑边偏移', () => {
    // 100x100 视频放进 200x100 容器：contain 后左右各 50px 黑边。
    expect(
      scaleBBox({ x1: 10, y1: 20, x2: 30, y2: 40 }, 100, 100, 200, 100),
    ).toEqual({ x1: 60, y1: 20, x2: 80, y2: 40 })
  })

  it('纵向 letterbox 时叠加顶部黑边偏移', () => {
    // 640x360 视频放进 640x480 容器：scale=1，上下各 60px 黑边。
    expect(
      scaleBBox({ x1: 0, y1: 0, x2: 640, y2: 360 }, 640, 360, 640, 480),
    ).toEqual({ x1: 0, y1: 60, x2: 640, y2: 420 })
  })

  it('4:3 视频放进 16:9 容器时按 contain 叠加左右黑边', () => {
    // 640x480 视频放进 640x360 容器：scale=0.75，显示宽 480，左右各 80px 黑边。
    expect(
      scaleBBox({ x1: 0, y1: 0, x2: 640, y2: 480 }, 640, 480, 640, 360),
    ).toEqual({ x1: 80, y1: 0, x2: 560, y2: 360 })
  })

  it('缩小显示区域时坐标按比例换算', () => {
    expect(
      scaleBBox({ x1: 10, y1: 20, x2: 30, y2: 40 }, 200, 200, 100, 100),
    ).toEqual({ x1: 5, y1: 10, x2: 15, y2: 20 })
  })
})

describe('filterDetections', () => {
  it('按最低置信度过滤，阈值本身保留，原顺序不变', () => {
    const frame = makeFrame({
      detections: [
        makeDetection({ detection_id: 'a', confidence: 0.9 }),
        makeDetection({ detection_id: 'b', confidence: 0.5, class_name: 'car' }),
        makeDetection({ detection_id: 'c', confidence: 0.49 }),
      ],
    })

    expect(
      filterDetections(frame, 0.5).map(
        (detection: TestDetection) => detection.detection_id,
      ),
    ).toEqual(['a', 'b'])
  })

  it('类别与阈值同时生效', () => {
    const frame = makeFrame({
      detections: [
        makeDetection({ detection_id: 'person-high', class_name: 'person', confidence: 0.92 }),
        makeDetection({ detection_id: 'car-high', class_name: 'car', confidence: 0.88 }),
        makeDetection({ detection_id: 'person-low', class_name: 'person', confidence: 0.3 }),
      ],
    })

    expect(
      filterDetections(frame, 0.5, 'person').map(
        (detection: TestDetection) => detection.detection_id,
      ),
    ).toEqual(['person-high'])
    expect(
      filterDetections(frame, 0.3, 'person').map(
        (detection: TestDetection) => detection.detection_id,
      ),
    ).toEqual(['person-high', 'person-low'])
  })

  it('空帧或空检测返回空数组，不构造假目标', () => {
    expect(filterDetections(makeFrame(), 0.5)).toEqual([])
    expect(filterDetections(makeFrame(), 0.5, 'person')).toEqual([])
  })
})

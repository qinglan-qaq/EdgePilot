import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { enableAutoUnmount, flushPromises, mount } from '@vue/test-utils'
import { nextTick } from 'vue'

import VisionPlayer from '../src/components/vision/VisionPlayer.vue'

enableAutoUnmount(afterEach)

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

const FRAMES: TestFrame[] = [
  {
    frame_id: 0,
    timestamp_ms: 0,
    inference_ms: 40,
    detections: [
      {
        detection_id: '0-0',
        class_id: 0,
        class_name: 'person',
        confidence: 0.9,
        bbox: { x1: 10, y1: 10, x2: 50, y2: 90 },
      },
    ],
  },
  {
    frame_id: 5,
    timestamp_ms: 200,
    inference_ms: 42,
    detections: [
      {
        detection_id: '5-0',
        class_id: 2,
        class_name: 'car',
        confidence: 0.8,
        bbox: { x1: 0, y1: 0, x2: 320, y2: 240 },
      },
    ],
  },
]

interface MockCanvasContext {
  clearRect: ReturnType<typeof vi.fn>
  strokeRect: ReturnType<typeof vi.fn>
  fillRect: ReturnType<typeof vi.fn>
  fillText: ReturnType<typeof vi.fn>
  beginPath: ReturnType<typeof vi.fn>
  closePath: ReturnType<typeof vi.fn>
  stroke: ReturnType<typeof vi.fn>
  fill: ReturnType<typeof vi.fn>
  save: ReturnType<typeof vi.fn>
  restore: ReturnType<typeof vi.fn>
  setLineDash: ReturnType<typeof vi.fn>
  measureText: ReturnType<typeof vi.fn>
  strokeStyle: string
  fillStyle: string
  lineWidth: number
  font: string
  textBaseline: string
}

function makeContext(): MockCanvasContext {
  return {
    clearRect: vi.fn(),
    strokeRect: vi.fn(),
    fillRect: vi.fn(),
    fillText: vi.fn(),
    beginPath: vi.fn(),
    closePath: vi.fn(),
    stroke: vi.fn(),
    fill: vi.fn(),
    save: vi.fn(),
    restore: vi.fn(),
    setLineDash: vi.fn(),
    measureText: vi.fn(() => ({ width: 40 })),
    strokeStyle: '',
    fillStyle: '',
    lineWidth: 1,
    font: '',
    textBaseline: '',
  }
}

let context: MockCanvasContext
let rafQueue: FrameRequestCallback[]

/** jsdom 不实现 canvas 与 requestAnimationFrame，用可控队列替代，避免无限递归。 */
function mountPlayer(
  overrides: Partial<{
    src: string
    frames: TestFrame[]
    videoWidth: number
    videoHeight: number
    selectedId: string | null
  }> = {},
) {
  return mount(VisionPlayer, {
    attachTo: document.body,
    props: {
      src: '/vision/gate.mp4',
      frames: FRAMES,
      videoWidth: 640,
      videoHeight: 480,
      ...overrides,
    } as never,
  })
}

type PlayerWrapper = ReturnType<typeof mountPlayer>

function setDisplaySize(wrapper: PlayerWrapper, width: number, height: number): void {
  const video = wrapper.find('video').element
  const canvas = wrapper.find('canvas').element

  for (const element of [video, canvas]) {
    for (const [key, value] of [
      ['clientWidth', width],
      ['clientHeight', height],
      ['offsetWidth', width],
      ['offsetHeight', height],
    ] as const) {
      Object.defineProperty(element, key, { configurable: true, value })
    }
  }
}

function setCurrentTime(wrapper: PlayerWrapper, seconds: number): void {
  ;(wrapper.find('video').element as HTMLVideoElement).currentTime = seconds
}

function runRafQueue(): void {
  const pending = rafQueue
  rafQueue = []
  for (const callback of pending) {
    callback(performance.now())
  }
}

/** 排空 rAF 队列与微任务，让加载/尺寸事件后的绘制稳定下来。 */
async function settle(): Promise<void> {
  for (let index = 0; index < 3; index += 1) {
    await nextTick()
    runRafQueue()
    await flushPromises()
  }
}

beforeEach(() => {
  context = makeContext()
  rafQueue = []

  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue(
    context as never,
  )
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
    rafQueue.push(callback)
    return rafQueue.length
  })
  vi.stubGlobal('cancelAnimationFrame', vi.fn())
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('VisionPlayer', () => {
  it('加载元数据后按当前时间绘制目标框与类别标签', async () => {
    const wrapper = mountPlayer()
    setDisplaySize(wrapper, 640, 480)
    setCurrentTime(wrapper, 0.1)

    await wrapper.find('video').trigger('loadedmetadata')
    await settle()

    const canvas = wrapper.find('canvas').element as HTMLCanvasElement
    expect(canvas.width).toBe(640)
    expect(canvas.height).toBe(480)
    expect(context.clearRect).toHaveBeenCalledWith(0, 0, 640, 480)
    expect(context.strokeRect).toHaveBeenCalledWith(10, 10, 40, 80)
    expect(context.fillText).toHaveBeenCalledWith(
      expect.stringContaining('person'),
      expect.any(Number),
      expect.any(Number),
    )
    expect(context.strokeStyle).toBe('#409eff')
    expect(context.lineWidth).toBe(2)
  })

  it('letterbox 后目标框带黑边偏移，与视频坐标系对齐', async () => {
    // 100x100 视频放进 200x100 容器：contain 后左右各 50px 黑边。
    const wrapper = mountPlayer({ videoWidth: 100, videoHeight: 100 })
    setDisplaySize(wrapper, 200, 100)
    setCurrentTime(wrapper, 0)

    await wrapper.find('video').trigger('loadedmetadata')
    await settle()

    expect(context.strokeRect).toHaveBeenCalledWith(60, 10, 40, 80)
  })

  it('timeupdate 按 currentTime 切换采样帧，seeked 同样触发重绘', async () => {
    const wrapper = mountPlayer()
    setDisplaySize(wrapper, 640, 480)
    setCurrentTime(wrapper, 0.1)

    await wrapper.find('video').trigger('loadedmetadata')
    await settle()

    context.fillText.mockClear()
    context.strokeRect.mockClear()

    // 300ms 落在第二个采样帧（200ms）之后，应绘制 car。
    setCurrentTime(wrapper, 0.3)
    await wrapper.find('video').trigger('timeupdate')
    await settle()

    expect(context.fillText).toHaveBeenCalledWith(
      expect.stringContaining('car'),
      expect.any(Number),
      expect.any(Number),
    )
    expect(context.fillText).not.toHaveBeenCalledWith(
      expect.stringContaining('person'),
      expect.any(Number),
      expect.any(Number),
    )
    expect(context.strokeRect).toHaveBeenCalledWith(0, 0, 320, 240)

    context.fillText.mockClear()
    context.strokeRect.mockClear()

    // seek 回 50ms，应重新回到第一个采样帧。
    setCurrentTime(wrapper, 0.05)
    await wrapper.find('video').trigger('seeked')
    await settle()

    expect(context.fillText).toHaveBeenCalledWith(
      expect.stringContaining('person'),
      expect.any(Number),
      expect.any(Number),
    )
    expect(context.strokeRect).toHaveBeenCalledWith(10, 10, 40, 80)
  })

  it('窗口尺寸变化后重算 canvas 尺寸与缩放坐标', async () => {
    const wrapper = mountPlayer()
    setDisplaySize(wrapper, 640, 480)
    setCurrentTime(wrapper, 0)

    await wrapper.find('video').trigger('loadedmetadata')
    await settle()
    expect(context.strokeRect).toHaveBeenCalledWith(10, 10, 40, 80)

    context.clearRect.mockClear()
    context.strokeRect.mockClear()

    setDisplaySize(wrapper, 320, 240)
    window.dispatchEvent(new Event('resize'))
    await settle()

    const canvas = wrapper.find('canvas').element as HTMLCanvasElement
    expect(canvas.width).toBe(320)
    expect(canvas.height).toBe(240)
    expect(context.clearRect).toHaveBeenCalledWith(0, 0, 320, 240)
    expect(context.strokeRect).toHaveBeenCalledWith(5, 5, 20, 40)
  })

  it('没有帧数据时只清空画布，不绘制任何目标框', async () => {
    const wrapper = mountPlayer({ frames: [] })
    setDisplaySize(wrapper, 640, 480)
    setCurrentTime(wrapper, 0)

    await wrapper.find('video').trigger('loadedmetadata')
    await settle()

    expect(context.clearRect).toHaveBeenCalled()
    expect(context.strokeRect).not.toHaveBeenCalled()
    expect(context.fillText).not.toHaveBeenCalled()
    expect(context.fillRect).not.toHaveBeenCalled()
  })

  it('视频加载失败时显示明确错误，且不再绘制目标框', async () => {
    const wrapper = mountPlayer()
    setDisplaySize(wrapper, 640, 480)
    await settle()

    context.strokeRect.mockClear()
    context.fillText.mockClear()

    await wrapper.find('video').trigger('error')
    await settle()

    expect(wrapper.text()).toMatch(/视频.*失败|失败.*视频|无法播放/)
    expect(context.strokeRect).not.toHaveBeenCalled()
    expect(context.fillText).not.toHaveBeenCalled()
  })

  it('selectedId 命中的目标使用高亮描边与更粗线宽', async () => {
    const wrapper = mountPlayer({ selectedId: '0-0' })
    setDisplaySize(wrapper, 640, 480)
    setCurrentTime(wrapper, 0)

    await wrapper.find('video').trigger('loadedmetadata')
    await settle()

    expect(context.strokeStyle).toBe('#f56c6c')
    expect(context.lineWidth).toBe(3)
  })
})

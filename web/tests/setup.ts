/**
 * Vitest 全局环境补丁。
 * jsdom 不实现 ResizeObserver / matchMedia，而 Vue Flow 与 Element Plus 的
 * 部分组件会直接使用它们；这里提供最小可用实现，避免测试被环境缺失阻断。
 */

class ResizeObserverStub implements ResizeObserver {
  observe(): void {
    // 测试环境不需要真实尺寸回调
  }

  unobserve(): void {
    // 测试环境不需要真实尺寸回调
  }

  disconnect(): void {
    // 测试环境不需要真实尺寸回调
  }
}

if (typeof globalThis.ResizeObserver === 'undefined') {
  Object.defineProperty(globalThis, 'ResizeObserver', {
    configurable: true,
    writable: true,
    value: ResizeObserverStub,
  })
}

if (typeof window !== 'undefined' && typeof window.matchMedia !== 'function') {
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }),
  })
}

// jsdom 的布局尺寸恒为 0；Vue Flow 会据此发出视口尺寸告警。
// 给 HTMLElement 一个稳定的非零尺寸，保证图容器可以正常初始化。
if (typeof HTMLElement !== 'undefined') {
  Object.defineProperty(HTMLElement.prototype, 'offsetWidth', {
    configurable: true,
    get() {
      return 800
    },
  })
  Object.defineProperty(HTMLElement.prototype, 'offsetHeight', {
    configurable: true,
    get() {
      return 600
    },
  })
}

// Vitest 默认不处理组件里的 CSS import，Vue Flow 会因此误报“样式未加载”。
// 这里补齐它检查的关键样式，避免测试输出出现无意义的告警。
if (typeof document !== 'undefined') {
  const style = document.createElement('style')
  style.textContent = '.vue-flow__pane { z-index: 1; }'
  document.head.appendChild(style)
}

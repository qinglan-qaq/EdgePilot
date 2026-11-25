/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端 API 基础路径，默认 /api/v1 */
  readonly VITE_API_BASE_URL?: string
  /** 开发服务器 /api 代理目标，默认 http://localhost:8000 */
  readonly VITE_PROXY_TARGET?: string
}

interface ImportMeta {
  readonly env: ImportMetaEnv
}

declare module '*.vue' {
  import type { DefineComponent } from 'vue'

  const component: DefineComponent<Record<string, unknown>, Record<string, unknown>, unknown>
  export default component
}

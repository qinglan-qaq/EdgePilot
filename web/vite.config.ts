import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { loadEnv } from 'vite'
import { defineConfig } from 'vitest/config'

export default defineConfig(({ mode }) => {
  // 允许通过 .env / 环境变量覆盖开发代理目标；默认用 IPv4，
  // 避免 Windows 上 Node 把 localhost 解析到 ::1 而 Docker 只监听 IPv4。
  const env = loadEnv(mode, process.cwd(), '')
  const proxyTarget = env.VITE_PROXY_TARGET || 'http://127.0.0.1:8000'

  return {
    plugins: [vue()],
    resolve: {
      alias: {
        '@': fileURLToPath(new URL('./src', import.meta.url)),
      },
    },
    server: {
      port: 5173,
      proxy: {
        '/api': {
          target: proxyTarget,
          changeOrigin: true,
        },
      },
    },
    test: {
      environment: 'jsdom',
      globals: true,
      include: ['tests/**/*.spec.ts'],
      restoreMocks: true,
      setupFiles: ['./tests/setup.ts'],
    },
  }
})

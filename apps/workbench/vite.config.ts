import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

const runtimeProxyTarget =
  process.env.VITE_RUNTIME_PROXY_TARGET ?? 'http://127.0.0.1:8787'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  cacheDir: '.vite-cache',
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: false,
    proxy: {
      '/health': runtimeProxyTarget,
      '/api': runtimeProxyTarget,
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: './src/setupTests.ts',
    exclude: ['e2e/**', 'node_modules/**', 'dist/**'],
    // The full component suite is memory-heavy on Windows. Keeping two
    // workers preserves parallelism without random worker exits/timeouts.
    maxWorkers: 2,
    // 整套跑满时单个组件用例偶发超过默认 5 秒（RunDrawer 单跑 0.1 秒、整套里
    // 曾 5 秒超时），放宽到 15 秒只为不把负载抖动误报成回归。
    testTimeout: 15_000,
  },
})

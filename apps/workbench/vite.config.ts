import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'

const runtimeProxyTarget =
  process.env.VITE_RUNTIME_PROXY_TARGET ?? 'http://127.0.0.1:8787'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
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
  },
})

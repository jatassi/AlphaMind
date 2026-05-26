import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'
import { defineConfig } from 'vite'

// FastAPI dev server runs on 127.0.0.1:8080 (config/command-center.yaml `bind`
// block). Vite serves the SPA on 5173 and proxies the API + SSE + auth surfaces
// through to FastAPI so dev mode mirrors production's single-port shape without
// running `bun run build` on every change.
const API_TARGET = 'http://127.0.0.1:8080'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': { target: API_TARGET, changeOrigin: true },
      '/auth': { target: API_TARGET, changeOrigin: true },
      '/events': { target: API_TARGET, changeOrigin: true },
      '/healthz': { target: API_TARGET, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    chunkSizeWarningLimit: 600,
  },
})

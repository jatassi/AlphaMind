import react from '@vitejs/plugin-react'
import path from 'node:path'
import { defineConfig } from 'vitest/config'

// Vitest config kept separate from vite.config.ts so the SPA build doesn't
// pull testing-library + jsdom into the production bundle. Per ALP-128
// § Pre-resolved (K), Vitest is the only frontend test runner.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/__tests__/setup.ts'],
    include: ['src/**/__tests__/**/*.test.{ts,tsx}'],
  },
})

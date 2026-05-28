import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import path from 'node:path'
import { defineConfig } from 'vitest/config'

// FastAPI dev server runs on 127.0.0.1:8080 (config/command-center.yaml `bind`
// block). Vite serves the SPA on 5173 and proxies the API + SSE + auth surfaces
// through to FastAPI so dev mode mirrors production's single-port shape without
// running `bun run build` on every change.
const API_TARGET = 'http://127.0.0.1:8080'

// LAN dev server notes (ALP-729 / 03c — Frontend dev server LAN notes for ALP-724).
//
// If you want to run `bun run dev` and reach the Vite SPA from another machine
// on the LAN (e.g. quick cross-machine smoke-testing during development of the
// command-center frontend):
//
//   - Temporarily change `server.host` from '127.0.0.1' to `true` (or a
//     specific LAN IP / '0.0.0.0') so Vite listens beyond loopback.
//   - You may also need to adjust `API_TARGET` (and/or the proxy changeOrigin
//     behavior) if your FastAPI backend is bound to a non-loopback address
//     (see `bind` + `access` blocks in command-center.yaml after ALP-725/726).
//
// HMR caveats:
//   - Vite's HMR websocket (used for live reload / fast refresh) frequently
//     requires additional `server.hmr` configuration (host, clientPort,
//     protocol) when the browser is not on the same machine; the default
//     assumes a same-origin loopback dev session.
//   - Browser-enforced security (WebSocket origin checks, cookies, mixed
//     content) will be governed by the origin the backend reports via its
//     re-wired WebAuthn resolver (ALP-726) and the `webauthn.relying_party_id`
//     + `access:` host in the YAMLs.
//
// For *real* daily LAN operator use of the Command Center UI:
//   - Prefer the *built* SPA bundle: run `bun run build` (produces dist/)
//     and let the production command-center daemon serve it statically on
//     the configured `access` origin (http://<lan-host>:8080/ etc.).
//   - The production path has no dev-server HMR/websocket surface, produces
//     the exact bundle operators will use, and aligns cleanly with the
//     backend's origin / cookie / passkey expectations.
//
// The committed defaults remain intentionally loopback-only for safe local
// development. Temporary LAN-dev edits to host should never be committed.
// See the approved LAN plan (§5, §8 story 03c) and RUNBOOK_command_center.md
// for production LAN wiring details.

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
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/__tests__/setup.ts'],
    include: ['src/**/__tests__/**/*.test.{ts,tsx}'],
  },
})

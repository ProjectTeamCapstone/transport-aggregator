import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // The API runs separately on :8000. Proxying in dev means the browser sees
  // one origin, so there is no CORS configuration to get wrong.
  server: { port: 5173, proxy: { '/api': { target: 'http://127.0.0.1:8000', rewrite: p => p.replace(/^\/api/, '') } } },
  test: { environment: 'jsdom', globals: true, setupFiles: './src/setupTests.js' },
})

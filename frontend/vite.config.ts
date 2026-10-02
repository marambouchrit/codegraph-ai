import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // Cytoscape is imported lazily (graph tab only): pre-bundle it when the dev server starts,
  // so its first import does not wait for (or fail on) dependency optimization.
  optimizeDeps: { include: ['cytoscape'] },
  test: {
    environment: 'jsdom',
    pool: 'threads', // worker threads start reliably on Windows (forked processes can time out)
    setupFiles: ['./src/test/setup.ts'],
  },
})

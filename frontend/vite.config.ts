import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Cytoscape is imported lazily (graph tab only): pre-bundle it when the dev server starts,
  // so its first import does not wait for (or fail on) dependency optimization.
  optimizeDeps: { include: ['cytoscape'] },
})

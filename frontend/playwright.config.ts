import { defineConfig } from '@playwright/test'

// End-to-end test of the main workflow in a real browser, against the real stack.
// Prerequisites (see README, "Testing and evaluation"): Neo4j and Qdrant started, the backend
// running on http://localhost:8000 with LLM_API_KEY set, and Microsoft Edge installed.
export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.e2e.ts',
  timeout: 15 * 60_000, // the first analysis loads the embedding model
  workers: 1,
  reporter: 'list',
  use: {
    baseURL: 'http://localhost:5173',
    channel: 'msedge', // the installed browser: nothing to download
    viewport: { width: 1280, height: 1000 },
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:5173',
    reuseExistingServer: true,
  },
})

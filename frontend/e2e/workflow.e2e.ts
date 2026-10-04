// The main workflow in a real browser: import -> analyze -> chat -> graph -> impact -> insights.
// Everything is real: backend, Neo4j, Qdrant, embedding model and LLM. Run with `npm run e2e`.
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { expect, test } from '@playwright/test'

const API = process.env.VITE_API_URL ?? 'http://localhost:8000'
const FIXTURE = path.join(path.dirname(fileURLToPath(import.meta.url)), 'fixtures', 'auth-service.zip')

let projectId: string | undefined

test.afterAll(async ({ request }) => {
  // Removes the imported source code. Its graph and vectors (17 nodes, 15 chunks) stay in the
  // databases: the application has no endpoint that deletes them.
  if (projectId) await request.delete(`${API}/projects/${projectId}`)
})

test('import, analyze, chat, graph, impact and insights', async ({ page, request }) => {
  const health = await request.get(`${API}/health`).catch(() => null)
  expect(health?.ok(), `the backend must be running on ${API}`).toBeTruthy()

  // 1-2. Open the application and import a small project (4 Python files).
  await page.goto('/')
  await expect(page.getByText('Backend online')).toBeVisible()
  await page.getByLabel('ZIP archive').setInputFiles(FIXTURE)
  await page.getByRole('button', { name: 'Upload ZIP' }).click()
  await page.waitForURL(/\/projects\/[0-9a-f]{32}$/)
  projectId = page.url().split('/').pop()
  await expect(page.getByText('Not analyzed', { exact: true })).toBeVisible()

  // 3-4. Analyze, and wait for the background job to finish.
  await page.getByRole('button', { name: 'Analyze Project' }).click()
  const badge = page.locator('.analysis-head .badge').first()
  await expect(badge).toHaveText(/^(Ready|Failed)$/, { timeout: 10 * 60_000 })
  await expect(badge).toHaveText('Ready')

  // 5-7. Ask a question: an answer with at least one source and one citation.
  await page.getByLabel('Your question').fill('How is authentication implemented?')
  await page.getByRole('button', { name: 'Send' }).click()
  const answer = page.locator('.message.assistant .markdown').first()
  await expect(answer).toBeVisible({ timeout: 180_000 })
  await expect(answer).not.toBeEmpty()
  expect(await page.locator('.message.assistant .source').count()).toBeGreaterThan(0)
  expect(await page.locator('.message.assistant button.cite').count()).toBeGreaterThan(0)

  // 8-9. Knowledge graph: drawn, then a node selected.
  await page.getByRole('tab', { name: 'Knowledge Graph' }).click()
  await expect(page.getByText(/Complete graph|Partial graph/)).toBeVisible({ timeout: 60_000 })
  await page.getByLabel('Find a node').fill('verify_password')
  await page.getByRole('button', { name: 'Find', exact: true }).click()
  const details = page.getByRole('complementary')
  await expect(details).toContainText('verify_password')

  // 10. Impact analysis: `AuthService.login` calls `verify_password`.
  await page.getByRole('button', { name: 'Impact analysis' }).click()
  await expect(page.locator('.impact-summary')).toBeVisible({ timeout: 60_000 })
  await expect(page.locator('.impact-level').first()).toContainText('login')

  // 11-12. Dependency analysis.
  await page.getByRole('tab', { name: 'Insights' }).click()
  await expect(page.locator('.insight-summary')).toContainText('file-to-file dependencies', {
    timeout: 60_000,
  })

  // 13-14. Architecture summary: facts from the graph, then the LLM's summary of them.
  await page.getByRole('button', { name: 'Generate architecture summary' }).click()
  await expect(page.getByText('Facts from the graph')).toBeVisible({ timeout: 180_000 })
  expect(await page.locator('.facts li').count()).toBeGreaterThan(0)
  await expect(page.locator('.architecture-summary .markdown')).not.toBeEmpty()
})

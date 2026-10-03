import { describe, expect, it, vi } from 'vitest'
import { ARCHITECTURE, DEPENDENCIES, GRAPH, IMPACT, json, mockApi, PROJECT, QUEUED, READY } from '../test/mockApi'
import {
  analyzeProject,
  ApiError,
  askQuestion,
  getAnalysis,
  getArchitecture,
  getDependencies,
  getGraph,
  getImpact,
  getProject,
  importFromGithub,
  uploadProjectZip,
} from './api'

describe('api requests match the backend contract', () => {
  it('imports from GitHub with {url}', async () => {
    const calls = mockApi({ 'POST /projects/github': () => json(PROJECT, 201) })

    await expect(importFromGithub('https://github.com/o/r')).resolves.toEqual(PROJECT)
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({ url: 'https://github.com/o/r' })
  })

  it('uploads a ZIP as multipart field "file"', async () => {
    const calls = mockApi({ 'POST /projects/zip': () => json(PROJECT, 201) })
    const file = new File(['zip'], 'auth.zip', { type: 'application/zip' })

    await uploadProjectZip(file)
    const body = calls[0].init?.body as FormData
    expect(body.get('file')).toBeInstanceOf(File)
    expect((body.get('file') as File).name).toBe('auth.zip')
  })

  it('starts an analysis with a POST and no body, and gets the queued job back', async () => {
    const calls = mockApi({ [`POST /projects/${PROJECT.id}/analyze`]: () => json(QUEUED, 202) })

    await expect(analyzeProject(PROJECT.id)).resolves.toEqual(QUEUED)
    expect(calls[0].init?.body).toBeUndefined()
    expect(String(vi.mocked(fetch).mock.calls[0][0])).toMatch(/\/analyze$/)

    await analyzeProject(PROJECT.id, true)
    expect(String(vi.mocked(fetch).mock.calls[1][0])).toMatch(/\/analyze\?full=true$/)
  })

  it('reads the impact of an entity, with its ID safely encoded', async () => {
    mockApi({ [`GET /projects/${PROJECT.id}/analysis/impact`]: () => json(IMPACT) })
    const entityId = `${PROJECT.id}:src/a b.py:User.save&x=1`

    await expect(getImpact(PROJECT.id, entityId, 2)).resolves.toEqual(IMPACT)
    const url = new URL(String(vi.mocked(fetch).mock.calls[0][0]))
    expect(url.searchParams.get('entity_id')).toBe(entityId) // not split by "&" or spaces
    expect(url.searchParams.get('depth')).toBe('2')
    expect([...url.searchParams.keys()]).toEqual(['entity_id', 'depth'])
  })

  it('reads the dependency analysis and the architecture overview', async () => {
    const calls = mockApi({
      [`GET /projects/${PROJECT.id}/analysis/dependencies`]: () => json(DEPENDENCIES),
      [`GET /projects/${PROJECT.id}/analysis/architecture`]: () => json(ARCHITECTURE),
    })

    await expect(getDependencies(PROJECT.id)).resolves.toEqual(DEPENDENCIES)
    await expect(getArchitecture(PROJECT.id)).resolves.toEqual(ARCHITECTURE)
    expect(calls.map((call) => call.method)).toEqual(['GET', 'GET'])
  })

  it('reads the analysis state', async () => {
    const calls = mockApi({ [`GET /projects/${PROJECT.id}/analysis`]: () => json(READY) })

    await expect(getAnalysis(PROJECT.id)).resolves.toEqual(READY)
    expect(calls[0].method).toBe('GET')
  })

  it('reads the graph, with an optional node limit', async () => {
    mockApi({ [`GET /projects/${PROJECT.id}/graph`]: () => json(GRAPH) })

    await expect(getGraph(PROJECT.id)).resolves.toEqual(GRAPH)
    await getGraph(PROJECT.id, 300)
    const urls = vi.mocked(fetch).mock.calls.map(([url]) => String(url))
    expect(urls[0]).toMatch(/\/graph$/)
    expect(urls[1]).toMatch(/\/graph\?limit=300$/)
  })

  it('asks with {question} only', async () => {
    const calls = mockApi({ [`POST /projects/${PROJECT.id}/chat`]: () => json({}) })

    await askQuestion(PROJECT.id, 'Why?')
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({ question: 'Why?' })
  })
})

describe('api errors are safe, user-facing messages', () => {
  it('shows the backend message of an application error', async () => {
    mockApi({ [`GET /projects/${PROJECT.id}`]: () => json({ detail: 'Project not found: x' }, 404) })

    const error = await getProject(PROJECT.id).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect(error).toMatchObject({ status: 404, message: 'Project not found: x' })
  })

  it('summarizes a 422 validation error', async () => {
    mockApi({
      [`POST /projects/${PROJECT.id}/chat`]: () =>
        json({ detail: [{ loc: ['body', 'question'], msg: 'Value error, The question must not be empty.' }] }, 422),
    })

    await expect(askQuestion(PROJECT.id, ' ')).rejects.toThrow(
      'Please check your input. The question must not be empty.',
    )
  })

  it('never shows the body of a 500', async () => {
    mockApi({ 'GET /projects/x': () => json({ detail: 'Traceback ... password=secret' }, 500) })

    await expect(getProject('x')).rejects.toThrow('Something went wrong. Please try again.')
  })

  it('uses a generic message when an error has no detail', async () => {
    mockApi({ 'POST /projects/x/chat': () => new Response('Bad gateway', { status: 502 }) })

    await expect(askQuestion('x', 'q')).rejects.toThrow('The AI service did not return a valid response.')
  })

  it('explains when the backend is unreachable', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')))

    await expect(getProject('x')).rejects.toMatchObject({ status: 0, message: expect.stringContaining('Cannot reach the backend') })
  })
})

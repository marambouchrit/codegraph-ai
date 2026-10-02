import { describe, expect, it, vi } from 'vitest'
import { GRAPH, json, mockApi, PROJECT, READY } from '../test/mockApi'
import {
  analyzeProject,
  ApiError,
  askQuestion,
  getAnalysis,
  getGraph,
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

  it('analyzes with a POST and no body', async () => {
    const calls = mockApi({ [`POST /projects/${PROJECT.id}/analyze`]: () => json({}) })

    await analyzeProject(PROJECT.id)
    expect(calls[0].init?.body).toBeUndefined()
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

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter, Route, Routes, useParams } from 'react-router'
import { describe, expect, it } from 'vitest'
import { json, mockApi, PROJECT } from '../../test/mockApi'
import HomePage from './HomePage'

function ProjectRoute() {
  return <p>Opened project {useParams().projectId}</p>
}

function renderHome() {
  render(
    <MemoryRouter>
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/projects/:projectId" element={<ProjectRoute />} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('HomePage', () => {
  it('renders the title and lists the projects with their metadata', async () => {
    mockApi({ 'GET /projects': () => json([PROJECT]) })
    renderHome()

    expect(screen.getByRole('heading', { name: 'CodeGraph AI' })).toBeInTheDocument()
    expect(screen.getByText('AI-powered codebase analysis using GraphRAG')).toBeInTheDocument()
    const link = await screen.findByRole('link', { name: /auth-service/ })
    expect(link).toHaveAttribute('href', `/projects/${PROJECT.id}`)
    expect(link).toHaveTextContent('4 files')
    expect(link).toHaveTextContent('python')
  })

  it('shows an empty state', async () => {
    mockApi({ 'GET /projects': () => json([]) })
    renderHome()

    expect(await screen.findByText('No projects yet.')).toBeInTheDocument()
  })

  it('shows an error when the projects cannot be loaded', async () => {
    mockApi({ 'GET /projects': () => json({ detail: 'boom' }, 500) })
    renderHome()

    expect(await screen.findByRole('alert')).toHaveTextContent('Could not load the projects.')
  })

  it('imports from GitHub and opens the project', async () => {
    const calls = mockApi({
      'GET /projects': () => json([]),
      'POST /projects/github': () => json(PROJECT, 201),
    })
    renderHome()

    await userEvent.type(screen.getByLabelText('GitHub repository URL'), 'https://github.com/o/r')
    await userEvent.click(screen.getByRole('button', { name: 'Import from GitHub' }))

    expect(await screen.findByText(`Opened project ${PROJECT.id}`)).toBeInTheDocument()
    expect(calls.some((call) => call.path === '/projects/github')).toBe(true)
  })

  it('shows the backend error of a failed GitHub import', async () => {
    mockApi({
      'GET /projects': () => json([]),
      'POST /projects/github': () => json({ detail: 'Not a valid GitHub repository URL.' }, 400),
    })
    renderHome()

    await userEvent.type(screen.getByLabelText('GitHub repository URL'), 'https://example.com/x')
    await userEvent.click(screen.getByRole('button', { name: 'Import from GitHub' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Not a valid GitHub repository URL.')
  })

  it('uploads a ZIP and opens the project', async () => {
    mockApi({
      'GET /projects': () => json([]),
      'POST /projects/zip': () => json(PROJECT, 201),
    })
    renderHome()

    const file = new File(['zip'], 'auth.zip', { type: 'application/zip' })
    await userEvent.upload(screen.getByLabelText('ZIP archive'), file)
    await userEvent.click(screen.getByRole('button', { name: 'Upload ZIP' }))

    expect(await screen.findByText(`Opened project ${PROJECT.id}`)).toBeInTheDocument()
  })

  it('asks for a file before uploading', async () => {
    const calls = mockApi({ 'GET /projects': () => json([]) })
    renderHome()

    await userEvent.click(screen.getByRole('button', { name: 'Upload ZIP' }))

    expect(screen.getByRole('alert')).toHaveTextContent('Choose a ZIP file first.')
    expect(calls.every((call) => call.path !== '/projects/zip')).toBe(true)
  })
})

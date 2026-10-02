import { useState, type FormEvent } from 'react'
import { useNavigate } from 'react-router'
import { importFromGithub, uploadProjectZip } from '../../services/api'
import type { Project } from '../../types/api'
import { errorText } from '../../utils/format'
import './ProjectImport.css'

type ImportState =
  | { state: 'idle' }
  | { state: 'loading' }
  | { state: 'error'; message: string }
  | { state: 'done'; project: Project }

/** Import a project from GitHub or a ZIP file, then open it. */
export default function ProjectImport() {
  const navigate = useNavigate()
  const [url, setUrl] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [github, setGithub] = useState<ImportState>({ state: 'idle' })
  const [zip, setZip] = useState<ImportState>({ state: 'idle' })
  const busy = github.state === 'loading' || zip.state === 'loading'

  function open(project: Project) {
    navigate(`/projects/${project.id}`, { state: { imported: true } })
  }

  async function onGithubSubmit(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    if (!url.trim()) {
      setGithub({ state: 'error', message: 'Enter a GitHub repository URL.' })
      return
    }
    setGithub({ state: 'loading' })
    try {
      const project = await importFromGithub(url.trim())
      setGithub({ state: 'done', project })
      open(project)
    } catch (error) {
      setGithub({ state: 'error', message: errorText(error) })
    }
  }

  async function onZipSubmit(event: FormEvent) {
    event.preventDefault()
    if (busy) return
    if (!file) {
      setZip({ state: 'error', message: 'Choose a ZIP file first.' })
      return
    }
    if (!file.name.toLowerCase().endsWith('.zip')) {
      setZip({ state: 'error', message: 'The file must be a .zip archive.' })
      return
    }
    setZip({ state: 'loading' })
    try {
      const project = await uploadProjectZip(file)
      setZip({ state: 'done', project })
      open(project)
    } catch (error) {
      setZip({ state: 'error', message: errorText(error) })
    }
  }

  return (
    <div className="import-grid">
      <form className="card" onSubmit={onGithubSubmit} aria-labelledby="github-title" noValidate>
        <h2 id="github-title">Import from GitHub</h2>
        <p className="card-subtitle">A public repository, cloned without running any of its code.</p>
        <label className="field-label" htmlFor="github-url">
          GitHub repository URL
        </label>
        <div className="import-row">
          <input
            id="github-url"
            className="input"
            type="url"
            inputMode="url"
            placeholder="https://github.com/owner/repository"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            disabled={busy}
          />
          <button className="button" type="submit" disabled={busy}>
            {github.state === 'loading' && <span className="spinner" aria-hidden="true" />}
            {github.state === 'loading' ? 'Importing…' : 'Import from GitHub'}
          </button>
        </div>
        <ImportFeedback status={github} loading="Cloning the repository…" />
      </form>

      <form className="card" onSubmit={onZipSubmit} aria-labelledby="zip-title" noValidate>
        <h2 id="zip-title">Upload a ZIP</h2>
        <p className="card-subtitle">An archive of your source code. Unsafe entries are rejected.</p>
        <label className="field-label" htmlFor="zip-file">
          ZIP archive
        </label>
        <div className="import-row">
          <input
            id="zip-file"
            className="input file-input"
            type="file"
            accept=".zip,application/zip"
            onChange={(event) => setFile(event.target.files?.[0] ?? null)}
            disabled={busy}
          />
          <button className="button" type="submit" disabled={busy}>
            {zip.state === 'loading' && <span className="spinner" aria-hidden="true" />}
            {zip.state === 'loading' ? 'Uploading…' : 'Upload ZIP'}
          </button>
        </div>
        <ImportFeedback status={zip} loading="Uploading and scanning the archive…" />
      </form>
    </div>
  )
}

function ImportFeedback({ status, loading }: { status: ImportState; loading: string }) {
  if (status.state === 'loading') {
    return (
      <p className="alert" role="status">
        {loading}
      </p>
    )
  }
  if (status.state === 'error') {
    return (
      <p className="alert error" role="alert">
        {status.message}
      </p>
    )
  }
  if (status.state === 'done') {
    return (
      <p className="alert ok" role="status">
        Imported {status.project.name}. Opening the project…
      </p>
    )
  }
  return null
}

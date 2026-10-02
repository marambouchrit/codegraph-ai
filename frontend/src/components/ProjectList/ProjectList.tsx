import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { getProjects } from '../../services/api'
import type { Project } from '../../types/api'
import { errorText, formatDate, sortedLanguages } from '../../utils/format'
import LoadingState from '../LoadingState/LoadingState'
import './ProjectList.css'

type ListState =
  | { state: 'loading' }
  | { state: 'error'; message: string }
  | { state: 'loaded'; projects: Project[] }

/** The imported projects, newest first; each opens its project page. */
export default function ProjectList() {
  const [list, setList] = useState<ListState>({ state: 'loading' })

  useEffect(() => {
    getProjects()
      .then((projects) =>
        setList({
          state: 'loaded',
          projects: [...projects].sort((a, b) => b.created_at.localeCompare(a.created_at)),
        }),
      )
      .catch((error: unknown) => setList({ state: 'error', message: errorText(error) }))
  }, [])

  return (
    <section className="card" aria-labelledby="projects-title">
      <h2 id="projects-title">Projects</h2>
      <p className="card-subtitle">Imported codebases. Open one to analyze it and ask questions.</p>

      {list.state === 'loading' && <LoadingState label="Loading projects…" />}
      {list.state === 'error' && (
        <p className="alert error" role="alert">
          Could not load the projects. {list.message}
        </p>
      )}
      {list.state === 'loaded' && list.projects.length === 0 && (
        <div className="empty">
          <p>No projects yet.</p>
          <p className="muted">Import a GitHub repository or upload a ZIP to get started.</p>
        </div>
      )}
      {list.state === 'loaded' && list.projects.length > 0 && (
        <ul className="project-list">
          {list.projects.map((project) => (
            <li key={project.id}>
              <Link className="project-item" to={`/projects/${project.id}`}>
                <div className="project-item-main">
                  <span className="project-name">{project.name}</span>
                  <span className="project-source muted">{project.source}</span>
                </div>
                <div className="project-item-meta">
                  <span className="badge">{project.source_type === 'github' ? 'GitHub' : 'ZIP'}</span>
                  <span className="muted">{project.file_count} files</span>
                  <span className="muted">
                    {sortedLanguages(project.languages)
                      .map(([language]) => language)
                      .join(', ')}
                  </span>
                  <span className="muted">{formatDate(project.created_at)}</span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

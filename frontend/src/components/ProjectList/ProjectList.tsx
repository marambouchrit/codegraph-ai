import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { getProjects } from '../../services/api'
import type { Project } from '../../types/api'
import { alert, badge, card, cardSubtitle, cardTitle, empty } from '../../ui'
import { errorText, formatDate, sortedLanguages } from '../../utils/format'
import LoadingState from '../LoadingState/LoadingState'

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
    <section className={card} aria-labelledby="projects-title">
      <h2 id="projects-title" className={cardTitle}>
        Projects
      </h2>
      <p className={cardSubtitle}>Imported codebases. Open one to analyze it and ask questions.</p>

      {list.state === 'loading' && <LoadingState label="Loading projects…" />}
      {list.state === 'error' && (
        <p className={alert.error} role="alert">
          Could not load the projects. {list.message}
        </p>
      )}
      {list.state === 'loaded' && list.projects.length === 0 && (
        <div className={empty}>
          <p>No projects yet.</p>
          <p className="text-muted">Import a GitHub repository or upload a ZIP to get started.</p>
        </div>
      )}
      {list.state === 'loaded' && list.projects.length > 0 && (
        <ul className="grid gap-2">
          {list.projects.map((project) => (
            <li key={project.id}>
              <Link
                className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 rounded-[10px] border border-line px-4 py-3.5 text-fg no-underline hover:border-accent hover:bg-accent-soft focus-visible:border-accent focus-visible:bg-accent-soft"
                to={`/projects/${project.id}`}
              >
                <div className="flex min-w-0 flex-col">
                  <span className="font-semibold">{project.name}</span>
                  <span className="truncate text-sm text-muted">{project.source}</span>
                </div>
                <div className="flex flex-wrap items-center gap-x-3.5 gap-y-1.5 text-sm">
                  <span className={badge.neutral}>{project.source_type === 'github' ? 'GitHub' : 'ZIP'}</span>
                  <span className="text-muted">{project.file_count} files</span>
                  <span className="text-muted">
                    {sortedLanguages(project.languages)
                      .map(([language]) => language)
                      .join(', ')}
                  </span>
                  <span className="text-muted">{formatDate(project.created_at)}</span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

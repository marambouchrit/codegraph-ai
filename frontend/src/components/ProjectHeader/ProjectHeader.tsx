import type { Project } from '../../types/api'
import { formatBytes, formatDate, sortedLanguages } from '../../utils/format'
import './ProjectHeader.css'

/** The project's name and the metadata returned by GET /projects/{id}. */
export default function ProjectHeader({ project }: { project: Project }) {
  return (
    <header className="card project-header">
      <div className="project-title">
        <h1>{project.name}</h1>
        <span className="badge">{project.source_type === 'github' ? 'GitHub' : 'ZIP'}</span>
      </div>
      <p className="project-source-line muted">
        {project.source_type === 'github' ? (
          <a href={project.source} target="_blank" rel="noopener noreferrer">
            {project.source}
          </a>
        ) : (
          project.source
        )}
      </p>
      <dl className="project-facts">
        <div>
          <dt>Source files</dt>
          <dd>{project.file_count.toLocaleString()}</dd>
        </div>
        <div>
          <dt>Size</dt>
          <dd>{formatBytes(project.total_size_bytes)}</dd>
        </div>
        <div>
          <dt>Imported</dt>
          <dd>{formatDate(project.created_at)}</dd>
        </div>
        <div>
          <dt>Languages</dt>
          <dd className="languages">
            {sortedLanguages(project.languages).map(([language, count]) => (
              <span key={language} className="badge">
                {language} · {count}
              </span>
            ))}
          </dd>
        </div>
      </dl>
    </header>
  )
}

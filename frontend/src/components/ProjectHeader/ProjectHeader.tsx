import type { Project } from '../../types/api'
import { badge, card } from '../../ui'
import { formatBytes, formatDate, sortedLanguages } from '../../utils/format'

const factLabel = 'text-xs tracking-wide text-muted uppercase'
const factValue = 'mt-0.5 font-semibold'

/** The project's name and the metadata returned by GET /projects/{id}. */
export default function ProjectHeader({ project }: { project: Project }) {
  return (
    <header className={card}>
      <div className="flex flex-wrap items-center gap-2.5">
        <h1 className="text-[clamp(1.4rem,3vw,1.8rem)] leading-tight font-bold tracking-tight wrap-anywhere">
          {project.name}
        </h1>
        <span className={badge.neutral}>{project.source_type === 'github' ? 'GitHub' : 'ZIP'}</span>
      </div>
      <p className="mt-1 mb-4 text-sm wrap-anywhere text-muted">
        {project.source_type === 'github' ? (
          <a href={project.source} target="_blank" rel="noopener noreferrer">
            {project.source}
          </a>
        ) : (
          project.source
        )}
      </p>
      <dl className="grid grid-cols-[repeat(auto-fit,minmax(150px,1fr))] gap-x-6 gap-y-3">
        <div>
          <dt className={factLabel}>Source files</dt>
          <dd className={factValue}>{project.file_count.toLocaleString()}</dd>
        </div>
        <div>
          <dt className={factLabel}>Size</dt>
          <dd className={factValue}>{formatBytes(project.total_size_bytes)}</dd>
        </div>
        <div>
          <dt className={factLabel}>Imported</dt>
          <dd className={factValue}>{formatDate(project.created_at)}</dd>
        </div>
        <div>
          <dt className={factLabel}>Languages</dt>
          <dd className={`${factValue} flex flex-wrap gap-1.5`}>
            {sortedLanguages(project.languages).map(([language, count]) => (
              <span key={language} className={badge.neutral}>
                {language} · {count}
              </span>
            ))}
          </dd>
        </div>
      </dl>
    </header>
  )
}

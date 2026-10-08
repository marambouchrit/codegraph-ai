import { useEffect, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router'
import AnalysisPanel from '../../components/AnalysisPanel/AnalysisPanel'
import Chat from '../../components/Chat/Chat'
import GraphPanel from '../../components/GraphViewer/GraphPanel'
import InsightsPanel from '../../components/Insights/InsightsPanel'
import LoadingState from '../../components/LoadingState/LoadingState'
import ProjectHeader from '../../components/ProjectHeader/ProjectHeader'
import { useAnalysis } from '../../hooks/useAnalysis'
import { ApiError, getProject } from '../../services/api'
import type { Project } from '../../types/api'
import { alert, card, page as pageLayout } from '../../ui'
import { errorText } from '../../utils/format'

type PageState =
  | { state: 'loading' }
  | { state: 'not-found' }
  | { state: 'error'; message: string }
  | { state: 'loaded'; project: Project }

type Tab = 'chat' | 'graph' | 'insights'

/** The project workspace: its metadata, the analysis, the chat, the graph and the insights. */
export default function ProjectPage() {
  const { projectId = '' } = useParams()
  // A new project ID remounts the workspace: its state (analysis, chat) starts fresh.
  return <ProjectWorkspace key={projectId} projectId={projectId} />
}

function ProjectWorkspace({ projectId }: { projectId: string }) {
  const location = useLocation()
  const justImported = (location.state as { imported?: boolean } | null)?.imported === true
  const [page, setPage] = useState<PageState>({ state: 'loading' })
  const { view, analyze } = useAnalysis(projectId)
  const [tab, setTab] = useState<Tab>('chat')
  // The graph and the insights are loaded the first time their tab is opened.
  const [opened, setOpened] = useState<ReadonlySet<Tab>>(new Set(['chat']))

  function openTab(next: Tab) {
    setTab(next)
    setOpened((current) => new Set(current).add(next))
  }

  // "Analyzed" means the databases hold a complete analysis (even while a new job runs).
  const report = view.state === 'loaded' ? view.status.analysis : null
  const analyzed = view.state === 'loading' ? null : report !== null
  const analyzedAt = report?.analyzed_at ?? null
  const notAnalyzed = view.state === 'loaded' && view.status.status === 'not_analyzed'

  useEffect(() => {
    let current = true // ignore a late answer after leaving the page
    getProject(projectId)
      .then((project) => current && setPage({ state: 'loaded', project }))
      .catch((error: unknown) => {
        if (!current) return
        if (error instanceof ApiError && error.status === 404) {
          setPage({ state: 'not-found' })
        } else {
          setPage({ state: 'error', message: errorText(error) })
        }
      })
    return () => {
      current = false
    }
  }, [projectId])

  return (
    <main className={pageLayout}>
      <Link to="/" className="justify-self-start text-sm text-muted no-underline hover:text-accent">
        ← All projects
      </Link>

      {page.state === 'loading' && <LoadingState label="Loading project…" />}
      {page.state === 'not-found' && (
        <section className={card}>
          <h1 className="text-2xl font-bold">Project not found</h1>
          <p className="text-muted">It may have been deleted, or the link is wrong.</p>
          <Link to="/">Back to projects</Link>
        </section>
      )}
      {page.state === 'error' && (
        <p className={alert.error} role="alert">
          Could not load the project. {page.message}
        </p>
      )}
      {page.state === 'loaded' && (
        <>
          {justImported && notAnalyzed && (
            <p className={alert.ok} role="status">
              Project imported. Next step: analyze it, then ask questions.
            </p>
          )}
          <ProjectHeader project={page.project} />
          <AnalysisPanel view={view} onAnalyze={analyze} />
          <div className="flex gap-1 border-b border-line" role="tablist" aria-label="Project tools">
            <TabButton id="chat" label="Chat" active={tab} onSelect={openTab} />
            <TabButton id="graph" label="Knowledge Graph" active={tab} onSelect={openTab} />
            <TabButton id="insights" label="Insights" active={tab} onSelect={openTab} />
          </div>
          {/* Tabs stay mounted once opened: switching keeps the chat history, the graph, the insights. */}
          <div id="panel-chat" role="tabpanel" aria-labelledby="tab-chat" hidden={tab !== 'chat'}>
            <Chat projectId={projectId} />
          </div>
          <div id="panel-graph" role="tabpanel" aria-labelledby="tab-graph" hidden={tab !== 'graph'}>
            {opened.has('graph') && <GraphPanel projectId={projectId} analyzed={analyzed} analyzedAt={analyzedAt} />}
          </div>
          <div id="panel-insights" role="tabpanel" aria-labelledby="tab-insights" hidden={tab !== 'insights'}>
            {opened.has('insights') && (
              <InsightsPanel projectId={projectId} analyzed={analyzed} analyzedAt={analyzedAt} />
            )}
          </div>
        </>
      )}
    </main>
  )
}

function TabButton({ id, label, active, onSelect }: { id: Tab; label: string; active: Tab; onSelect: (tab: Tab) => void }) {
  return (
    <button
      type="button"
      role="tab"
      id={`tab-${id}`}
      aria-controls={`panel-${id}`}
      aria-selected={active === id}
      className={`-mb-px cursor-pointer rounded-t-lg border px-4 py-2.5 font-semibold focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent ${
        active === id
          ? 'border-line border-b-page bg-page text-accent'
          : 'border-transparent text-muted hover:text-fg'
      }`}
      onClick={() => onSelect(id)}
    >
      {label}
    </button>
  )
}

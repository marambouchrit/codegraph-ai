import { useEffect, useState } from 'react'
import { Link, useLocation, useParams } from 'react-router'
import AnalysisPanel from '../../components/AnalysisPanel/AnalysisPanel'
import Chat from '../../components/Chat/Chat'
import GraphPanel from '../../components/GraphViewer/GraphPanel'
import LoadingState from '../../components/LoadingState/LoadingState'
import ProjectHeader from '../../components/ProjectHeader/ProjectHeader'
import { useAnalysis } from '../../hooks/useAnalysis'
import { ApiError, getProject } from '../../services/api'
import type { Project } from '../../types/api'
import { errorText } from '../../utils/format'
import './ProjectPage.css'

type PageState =
  | { state: 'loading' }
  | { state: 'not-found' }
  | { state: 'error'; message: string }
  | { state: 'loaded'; project: Project }

type Tab = 'chat' | 'graph'

/** The project workspace: its metadata, the analysis, the chat and the knowledge graph. */
export default function ProjectPage() {
  const { projectId = '' } = useParams()
  // A new project ID remounts the workspace: its state (analysis, chat) starts fresh.
  return <ProjectWorkspace key={projectId} projectId={projectId} />
}

function ProjectWorkspace({ projectId }: { projectId: string }) {
  const location = useLocation()
  const justImported = (location.state as { imported?: boolean } | null)?.imported === true
  const [page, setPage] = useState<PageState>({ state: 'loading' })
  const { analysis, analyze } = useAnalysis(projectId)
  const [tab, setTab] = useState<Tab>('chat')
  const [graphOpened, setGraphOpened] = useState(false) // load the graph on first view only

  function openTab(next: Tab) {
    setTab(next)
    if (next === 'graph') setGraphOpened(true)
  }

  const analyzed = analysis.state === 'loading' ? null : analysis.state === 'ready'
  const analyzedAt = analysis.state === 'ready' ? analysis.result.analyzed_at : null

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
    <main className="page project-page">
      <Link to="/" className="back-link">
        ← All projects
      </Link>

      {page.state === 'loading' && <LoadingState label="Loading project…" />}
      {page.state === 'not-found' && (
        <section className="card">
          <h1>Project not found</h1>
          <p className="muted">It may have been deleted, or the link is wrong.</p>
          <Link to="/">Back to projects</Link>
        </section>
      )}
      {page.state === 'error' && (
        <p className="alert error" role="alert">
          Could not load the project. {page.message}
        </p>
      )}
      {page.state === 'loaded' && (
        <>
          {justImported && analysis.state === 'not_analyzed' && (
            <p className="alert ok" role="status">
              Project imported. Next step: analyze it, then ask questions.
            </p>
          )}
          <ProjectHeader project={page.project} />
          <AnalysisPanel analysis={analysis} onAnalyze={analyze} />
          <div className="tabs" role="tablist" aria-label="Project tools">
            <TabButton id="chat" label="Chat" active={tab} onSelect={openTab} />
            <TabButton id="graph" label="Knowledge Graph" active={tab} onSelect={openTab} />
          </div>
          {/* Both stay mounted once opened: switching tabs keeps the chat history and the graph. */}
          <div id="panel-chat" role="tabpanel" aria-labelledby="tab-chat" hidden={tab !== 'chat'}>
            <Chat projectId={projectId} />
          </div>
          <div id="panel-graph" role="tabpanel" aria-labelledby="tab-graph" hidden={tab !== 'graph'}>
            {graphOpened && <GraphPanel projectId={projectId} analyzed={analyzed} analyzedAt={analyzedAt} />}
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
      className={`tab${active === id ? ' active' : ''}`}
      onClick={() => onSelect(id)}
    >
      {label}
    </button>
  )
}

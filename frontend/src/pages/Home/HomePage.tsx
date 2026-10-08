import ProjectImport from '../../components/ProjectImport/ProjectImport'
import ProjectList from '../../components/ProjectList/ProjectList'
import { page } from '../../ui'

export default function HomePage() {
  return (
    <main className={page}>
      <header className="pt-4 pb-2">
        <h1 className="text-[clamp(1.8rem,4vw,2.4rem)] leading-tight font-bold tracking-tight">CodeGraph AI</h1>
        <p className="mt-1.5 text-lg text-muted">AI-powered codebase analysis using GraphRAG</p>
      </header>
      <ProjectImport />
      <ProjectList />
    </main>
  )
}

import ProjectImport from '../../components/ProjectImport/ProjectImport'
import ProjectList from '../../components/ProjectList/ProjectList'
import './HomePage.css'

export default function HomePage() {
  return (
    <main className="page home">
      <header className="hero">
        <h1>CodeGraph AI</h1>
        <p>AI-powered codebase analysis using GraphRAG</p>
      </header>
      <ProjectImport />
      <ProjectList />
    </main>
  )
}

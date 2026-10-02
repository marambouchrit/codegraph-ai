import { Link, Route, Routes } from 'react-router'
import AppHeader from './components/AppHeader/AppHeader'
import HomePage from './pages/Home/HomePage'
import ProjectPage from './pages/Project/ProjectPage'

export default function App() {
  return (
    <>
      <AppHeader />
      <Routes>
        <Route path="/" element={<HomePage />} />
        <Route path="/projects/:projectId" element={<ProjectPage />} />
        <Route
          path="*"
          element={
            <main className="page">
              <h1>Page not found</h1>
              <Link to="/">Back to projects</Link>
            </main>
          }
        />
      </Routes>
    </>
  )
}

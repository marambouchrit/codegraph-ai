import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { getHealth } from '../../services/api'
import './AppHeader.css'

type Backend = 'checking' | 'online' | 'offline'

/** Top bar: the product name (link home) and whether the backend answers /health. */
export default function AppHeader() {
  const [backend, setBackend] = useState<Backend>('checking')

  useEffect(() => {
    getHealth()
      .then(() => setBackend('online'))
      .catch(() => setBackend('offline'))
  }, [])

  return (
    <header className="app-header">
      <div className="app-header-inner">
        <Link to="/" className="brand">
          <span className="brand-mark" aria-hidden="true">
            ◆
          </span>
          CodeGraph AI
        </Link>
        <span
          className={`badge ${backend === 'online' ? 'ok' : backend === 'offline' ? 'error' : ''}`}
          title="Backend API status"
        >
          <span className="dot" aria-hidden="true" />
          {backend === 'checking' && 'Checking backend…'}
          {backend === 'online' && 'Backend online'}
          {backend === 'offline' && 'Backend unreachable'}
        </span>
      </div>
    </header>
  )
}

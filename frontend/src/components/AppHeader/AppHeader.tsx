import { useEffect, useState } from 'react'
import { Link } from 'react-router'
import { getHealth } from '../../services/api'
import { badge } from '../../ui'

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
    <header className="border-b border-line bg-surface">
      <div className="mx-auto flex max-w-[1100px] items-center justify-between gap-3 px-4 py-3">
        <Link to="/" className="flex items-center gap-2 text-lg font-bold text-fg no-underline">
          <span className="text-accent" aria-hidden="true">
            ◆
          </span>
          CodeGraph AI
        </Link>
        <span
          className={backend === 'online' ? badge.ok : backend === 'offline' ? badge.error : badge.neutral}
          title="Backend API status"
        >
          <span className="size-2 rounded-full bg-current" aria-hidden="true" />
          {backend === 'checking' && 'Checking backend…'}
          {backend === 'online' && 'Backend online'}
          {backend === 'offline' && 'Backend unreachable'}
        </span>
      </div>
    </header>
  )
}

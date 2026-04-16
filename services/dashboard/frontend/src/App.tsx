import { useState } from 'react'
import MatchedBetting from './components/MatchedBetting'
import ModelStats from './components/ModelStats'
import PolymarketCards from './components/PolymarketCards'
import SportSignals from './components/SportSignals'

type Tab = 'sports' | 'poly' | 'matched' | 'stats'

export default function App() {
  const [tab, setTab] = useState<Tab>('sports')

  const tabs: { id: Tab; label: string }[] = [
    { id: 'sports', label: '⚽ Señales Deportivas' },
    { id: 'poly', label: '🔮 Polymarket' },
    { id: 'matched', label: '🧮 Matched Betting' },
    { id: 'stats', label: '📊 Estadísticas' },
  ]

  return (
    <div style={{ background: '#0A0A0A', minHeight: '100vh', color: '#FFFFFF', fontFamily: 'monospace' }}>
      {/* Header */}
      <header style={{ borderBottom: '1px solid #F7931A', padding: '16px 24px', display: 'flex', alignItems: 'center', gap: 16 }}>
        <span style={{ color: '#F7931A', fontWeight: 'bold', fontSize: 20 }}>⚡ Prediction Intelligence</span>
        <span style={{ color: '#888', fontSize: 12 }}>Crypto sin humo. Análisis real, opinión directa.</span>
      </header>

      {/* Tabs */}
      <nav style={{ display: 'flex', gap: 4, padding: '12px 24px', borderBottom: '1px solid #222' }}>
        {tabs.map(t => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            style={{
              background: tab === t.id ? '#F7931A' : '#1A1A1A',
              color: tab === t.id ? '#000' : '#FFF',
              border: 'none',
              borderRadius: 6,
              padding: '8px 16px',
              cursor: 'pointer',
              fontFamily: 'monospace',
              fontSize: 13,
            }}
          >
            {t.label}
          </button>
        ))}
      </nav>

      {/* Content */}
      <main style={{ padding: 24 }}>
        {tab === 'sports' && <SportSignals />}
        {tab === 'poly' && <PolymarketCards />}
        {tab === 'matched' && <MatchedBetting />}
        {tab === 'stats' && <ModelStats />}
      </main>
    </div>
  )
}

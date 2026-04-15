import { useApi } from '../hooks/useApi'

interface Prediction {
  match_id: string
  home_team: string
  away_team: string
  league: string
  match_date: string
  team_to_back: string
  odds: number
  edge: number
  confidence: number
  factors: Record<string, number>
  result: string | null
  correct: boolean | null
}

export default function SportSignals() {
  const { data, loading, error } = useApi<Prediction[]>('/api/predictions')

  if (loading) return <p style={{ color: '#888' }}>Cargando señales...</p>
  if (error) return <p style={{ color: '#F7931A' }}>Error: {error}</p>
  if (!data?.length) return <p style={{ color: '#888' }}>Sin señales activas.</p>

  return (
    <div>
      <h2 style={{ color: '#F7931A', marginBottom: 16 }}>⚽ Señales Deportivas</h2>
      {/* TODO: implementar cards completas en Sesion 7 */}
      {data.map(p => (
        <div key={p.match_id} style={{ background: '#1A1A1A', borderRadius: 8, padding: 16, marginBottom: 12, borderLeft: '3px solid #F7931A' }}>
          <div style={{ fontWeight: 'bold' }}>{p.home_team} vs {p.away_team}</div>
          <div style={{ color: '#888', fontSize: 12 }}>{p.league} · {p.match_date}</div>
          <div style={{ marginTop: 8 }}>
            <span style={{ color: '#F7931A' }}>Edge: +{(p.edge * 100).toFixed(1)}%</span>
            <span style={{ marginLeft: 16, color: '#888' }}>Conf: {(p.confidence * 100).toFixed(0)}%</span>
          </div>
        </div>
      ))}
    </div>
  )
}

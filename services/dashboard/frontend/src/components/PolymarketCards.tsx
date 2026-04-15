import { useApi } from '../hooks/useApi'

interface PolyPrediction {
  market_id: string
  question: string
  market_price_yes: number
  real_prob: number
  edge: number
  confidence: number
  trend: string
  recommendation: string
  volume_spike: boolean
  analyzed_at: string
}

export default function PolymarketCards() {
  const { data, loading, error } = useApi<PolyPrediction[]>('/api/poly')

  if (loading) return <p style={{ color: '#888' }}>Cargando mercados...</p>
  if (error) return <p style={{ color: '#F7931A' }}>Error: {error}</p>
  if (!data?.length) return <p style={{ color: '#888' }}>Sin mercados con edge detectado.</p>

  return (
    <div>
      <h2 style={{ color: '#F7931A', marginBottom: 16 }}>🔮 Polymarket</h2>
      {/* TODO: implementar cards completas en Sesion 7 */}
      {data.map(p => (
        <div key={p.market_id} style={{ background: '#1A1A1A', borderRadius: 8, padding: 16, marginBottom: 12, borderLeft: '3px solid #F7931A' }}>
          <div style={{ fontWeight: 'bold', marginBottom: 8 }}>{p.question}</div>
          <div style={{ display: 'flex', gap: 16, fontSize: 13 }}>
            <span>YES: {(p.market_price_yes * 100).toFixed(0)}%</span>
            <span style={{ color: '#F7931A' }}>Edge: +{(p.edge * 100).toFixed(0)}%</span>
            <span>{p.recommendation}</span>
            {p.volume_spike && <span style={{ color: '#F7931A' }}>🐋 Vol spike</span>}
          </div>
        </div>
      ))}
    </div>
  )
}

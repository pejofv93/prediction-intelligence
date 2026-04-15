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
  smart_money_detected: boolean
  key_factors: string[]
  reasoning: string
  analyzed_at: string
  alerted: boolean
}

function TrendBadge({ trend }: { trend: string }) {
  const map: Record<string, { color: string; icon: string }> = {
    IMPROVING: { color: '#00C853', icon: '↑' },
    DETERIORATING: { color: '#FF5252', icon: '↓' },
    STABLE: { color: '#888', icon: '→' },
    NO_DATA: { color: '#555', icon: '—' },
  }
  const t = map[trend] ?? map['NO_DATA']
  return (
    <span style={{ color: t.color, fontWeight: 'bold', fontSize: 12 }}>
      {t.icon} {trend}
    </span>
  )
}

function ProbBar({ label, value, color }: { label: string; value: number; color: string }) {
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
      <span style={{ width: 72, color: '#888' }}>{label}</span>
      <div style={{ flex: 1, background: '#333', borderRadius: 3, height: 6 }}>
        <div style={{ width: `${Math.round(value * 100)}%`, background: color, height: 6, borderRadius: 3 }} />
      </div>
      <span style={{ width: 40, textAlign: 'right', color: '#ccc' }}>{(value * 100).toFixed(1)}%</span>
    </div>
  )
}

function PolyCard({ p }: { p: PolyPrediction }) {
  const edgeColor = p.edge > 0.20 ? '#00C853' : p.edge > 0.12 ? '#F7931A' : '#888'
  const dateStr = p.analyzed_at
    ? new Date(p.analyzed_at).toLocaleDateString('es-ES', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
    : '—'
  const factors: string[] = Array.isArray(p.key_factors) ? p.key_factors : []

  return (
    <div style={{
      background: '#141414',
      border: '1px solid #2a2a2a',
      borderLeft: `3px solid ${edgeColor}`,
      borderRadius: 8,
      padding: '16px 20px',
      marginBottom: 12,
    }}>
      {/* Header */}
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 12, gap: 12 }}>
        <div style={{ fontWeight: 'bold', fontSize: 14, lineHeight: 1.4, flex: 1 }}>{p.question}</div>
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 4, flexShrink: 0 }}>
          {p.volume_spike && (
            <span style={{ background: '#F7931A22', color: '#F7931A', border: '1px solid #F7931A44', borderRadius: 4, padding: '1px 6px', fontSize: 10, fontWeight: 'bold' }}>
              🐋 VOL SPIKE
            </span>
          )}
          {p.smart_money_detected && (
            <span style={{ background: '#9C27B022', color: '#CE93D8', border: '1px solid #9C27B044', borderRadius: 4, padding: '1px 6px', fontSize: 10, fontWeight: 'bold' }}>
              🎯 SMART $
            </span>
          )}
          {p.alerted && (
            <span style={{ background: '#00C85322', color: '#00C853', border: '1px solid #00C85344', borderRadius: 4, padding: '1px 6px', fontSize: 10 }}>
              ✓ Alertado
            </span>
          )}
        </div>
      </div>

      {/* Metrics row */}
      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 12 }}>
        {[
          { label: 'RECOMENDACIÓN', value: p.recommendation, color: '#F7931A' },
          { label: 'EDGE', value: `+${(p.edge * 100).toFixed(1)}%`, color: edgeColor },
          { label: 'CONFIANZA', value: `${(p.confidence * 100).toFixed(0)}%`, color: '#ccc' },
        ].map(({ label, value, color }) => (
          <div key={label} style={{ background: '#1e1e1e', border: '1px solid #2e2e2e', borderRadius: 6, padding: '6px 12px', minWidth: 80 }}>
            <div style={{ color: '#555', fontSize: 10, marginBottom: 2 }}>{label}</div>
            <div style={{ color, fontWeight: 'bold', fontSize: 13 }}>{value}</div>
          </div>
        ))}
        <div style={{ background: '#1e1e1e', border: '1px solid #2e2e2e', borderRadius: 6, padding: '6px 12px' }}>
          <div style={{ color: '#555', fontSize: 10, marginBottom: 2 }}>TENDENCIA</div>
          <TrendBadge trend={p.trend || 'NO_DATA'} />
        </div>
      </div>

      {/* Probability bars */}
      <div style={{ borderTop: '1px solid #222', paddingTop: 10, marginBottom: 10 }}>
        <div style={{ color: '#444', fontSize: 11, marginBottom: 8, letterSpacing: 0.5 }}>PROBABILIDADES</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
          <ProbBar label="Precio mercado" value={p.market_price_yes ?? 0} color="#888" />
          <ProbBar label="Prob. real (IA)" value={p.real_prob ?? 0} color="#F7931A" />
        </div>
      </div>

      {/* Key factors */}
      {factors.length > 0 && (
        <div style={{ marginBottom: 10 }}>
          <div style={{ color: '#444', fontSize: 11, marginBottom: 6, letterSpacing: 0.5 }}>FACTORES CLAVE</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {factors.slice(0, 4).map((f, i) => (
              <span key={i} style={{ background: '#1e1e1e', border: '1px solid #333', borderRadius: 4, padding: '2px 8px', fontSize: 11, color: '#aaa' }}>
                {f}
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Reasoning */}
      {p.reasoning && (
        <div style={{ borderTop: '1px solid #1a1a1a', paddingTop: 8 }}>
          <div style={{ color: '#444', fontSize: 11, marginBottom: 4, letterSpacing: 0.5 }}>ANÁLISIS IA</div>
          <p style={{ color: '#666', fontSize: 12, margin: 0, lineHeight: 1.5 }}>
            {p.reasoning.length > 200 ? p.reasoning.slice(0, 200) + '…' : p.reasoning}
          </p>
        </div>
      )}

      <div style={{ color: '#444', fontSize: 11, marginTop: 10, textAlign: 'right' }}>
        Analizado: {dateStr}
      </div>
    </div>
  )
}

export default function PolymarketCards() {
  const { data, loading, error } = useApi<PolyPrediction[]>('/api/poly')

  if (loading) return <p style={{ color: '#888', padding: 24 }}>Cargando mercados...</p>
  if (error) return <p style={{ color: '#F7931A', padding: 24 }}>Error: {error}</p>
  if (!data?.length) return <p style={{ color: '#888', padding: 24 }}>Sin mercados con edge detectado.</p>

  const highEdge = data.filter(p => p.edge >= 0.20)
  const midEdge = data.filter(p => p.edge >= 0.12 && p.edge < 0.20)

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 20 }}>
        <h2 style={{ color: '#F7931A', margin: 0 }}>🔮 Polymarket</h2>
        <span style={{ color: '#888', fontSize: 13 }}>{data.length} mercados con edge</span>
      </div>

      {highEdge.length > 0 && (
        <>
          <div style={{ color: '#666', fontSize: 11, letterSpacing: 1, marginBottom: 10 }}>
            ALTO EDGE ≥20% ({highEdge.length})
          </div>
          {highEdge.map(p => <PolyCard key={p.market_id} p={p} />)}
        </>
      )}

      {midEdge.length > 0 && (
        <>
          <div style={{ color: '#666', fontSize: 11, letterSpacing: 1, margin: '20px 0 10px' }}>
            EDGE 12–20% ({midEdge.length})
          </div>
          {midEdge.map(p => <PolyCard key={p.market_id} p={p} />)}
        </>
      )}
    </div>
  )
}

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
  kelly_fraction?: number
  factors: Record<string, number>
  result: string | null
  correct: boolean | null
  sport?: string
  filtered_reason?: string | null
}

const LEAGUE_FLAGS: Record<string, string> = {
  PL: '🏴󠁧󠁢󠁥󠁮󠁧󠁿', PD: '🇪🇸', BL1: '🇩🇪', SA: '🇮🇹',
  NBA: '🏀', NFL: '🏈', MLB: '⚾', NHL: '🏒', UFC: '🥊',
}

function FactorBar({ label, value }: { label: string; value: number }) {
  const barColor = value > 0.6 ? '#00C853' : value > 0.45 ? '#F7931A' : '#FF5252'
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
      <span style={{ width: 52, color: '#888', textTransform: 'capitalize' }}>{label}</span>
      <div style={{ flex: 1, background: '#333', borderRadius: 3, height: 6 }}>
        <div style={{ width: `${Math.round(value * 100)}%`, background: barColor, height: 6, borderRadius: 3 }} />
      </div>
      <span style={{ width: 36, textAlign: 'right', color: '#ccc' }}>{(value * 100).toFixed(0)}%</span>
    </div>
  )
}

function PredictionCard({ p }: { p: Prediction }) {
  const flag = LEAGUE_FLAGS[p.league] || '🏆'
  const dateStr = p.match_date
    ? new Date(p.match_date).toLocaleDateString('es-ES', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
    : '—'
  const isObsolete = Boolean(p.filtered_reason)
  const accentColor = isObsolete ? '#555555' : p.correct === true ? '#00C853' : p.correct === false ? '#FF5252' : '#F7931A'
  const REASON_LABELS: Record<string, string> = {
    underdog_extremo: 'OBSOLETA · underdog extremo',
    away_zona_muerta: 'OBSOLETA · AWAY zona muerta',
    away_pd_ded:      'OBSOLETA · AWAY PD/DED',
    away_gate:        'OBSOLETA · AWAY gate',
  }
  const resultLabel = isObsolete
    ? (REASON_LABELS[p.filtered_reason!] ?? `OBSOLETA · ${p.filtered_reason}`)
    : p.correct === true ? '✓ Correcto' : p.correct === false ? '✗ Incorrecto' : 'Pendiente'
  const factors = p.factors || {}
  const hasFourFactors = ['poisson', 'elo', 'form', 'h2h'].every(k => k in factors)

  return (
    <div style={{ background: '#141414', border: '1px solid #2a2a2a', borderLeft: `3px solid ${accentColor}`, borderRadius: 8, padding: '16px 20px', marginBottom: 12 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 10 }}>
        <div>
          <div style={{ fontWeight: 'bold', fontSize: 15 }}>{p.home_team} vs {p.away_team}</div>
          <div style={{ color: '#666', fontSize: 12, marginTop: 2 }}>{flag} {p.league} · 📅 {dateStr}</div>
        </div>
        <span style={{ background: accentColor + '22', color: accentColor, border: `1px solid ${accentColor}44`, borderRadius: 4, padding: '2px 8px', fontSize: 11, fontWeight: 'bold', whiteSpace: 'nowrap' }}>
          {resultLabel}
        </span>
      </div>

      <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: hasFourFactors ? 12 : 0 }}>
        {[
          { label: 'APOSTAR A', value: p.team_to_back, color: '#F7931A' },
          { label: 'CUOTA', value: p.odds?.toFixed(2), color: '#fff' },
          { label: 'EDGE', value: `+${(p.edge * 100).toFixed(1)}%`, color: '#00C853' },
          { label: 'CONFIANZA', value: `${(p.confidence * 100).toFixed(0)}%`, color: '#ccc' },
          ...(p.kelly_fraction != null ? [{ label: 'KELLY', value: `${(p.kelly_fraction * 100).toFixed(1)}%`, color: '#ccc' }] : []),
        ].map(({ label, value, color }) => (
          <div key={label} style={{ background: '#1e1e1e', border: '1px solid #2e2e2e', borderRadius: 6, padding: '6px 12px', minWidth: 70 }}>
            <div style={{ color: '#555', fontSize: 10, marginBottom: 2 }}>{label}</div>
            <div style={{ color, fontWeight: 'bold', fontSize: 14 }}>{value}</div>
          </div>
        ))}
      </div>

      {hasFourFactors && (
        <div style={{ borderTop: '1px solid #222', paddingTop: 10 }}>
          <div style={{ color: '#444', fontSize: 11, marginBottom: 8, letterSpacing: 0.5 }}>SEÑALES DEL MODELO</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 5 }}>
            {['poisson', 'elo', 'form', 'h2h'].map(k => <FactorBar key={k} label={k} value={factors[k] ?? 0} />)}
          </div>
        </div>
      )}
    </div>
  )
}

export default function SportSignals() {
  const { data, loading, error } = useApi<Prediction[]>('/api/predictions')

  if (loading) return <p style={{ color: '#888', padding: 24 }}>Cargando señales...</p>
  if (error) return <p style={{ color: '#F7931A', padding: 24 }}>Error: {error}</p>
  if (!data?.length) return <p style={{ color: '#888', padding: 24 }}>Sin señales activas en este momento.</p>

  const pending  = data.filter(p => p.result === null && !p.filtered_reason)
  const obsolete = data.filter(p => p.result === null && Boolean(p.filtered_reason))
  const resolved = data.filter(p => p.result !== null)

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 20 }}>
        <h2 style={{ color: '#F7931A', margin: 0 }}>⚽ Señales Deportivas</h2>
        <span style={{ color: '#888', fontSize: 13 }}>{data.length} total</span>
      </div>
      {pending.length > 0 && (
        <>
          <div style={{ color: '#666', fontSize: 11, letterSpacing: 1, marginBottom: 10 }}>PENDIENTES ({pending.length})</div>
          {pending.map(p => <PredictionCard key={p.match_id} p={p} />)}
        </>
      )}
      {resolved.length > 0 && (
        <>
          <div style={{ color: '#666', fontSize: 11, letterSpacing: 1, margin: '20px 0 10px' }}>RESUELTAS ({resolved.length})</div>
          {resolved.map(p => <PredictionCard key={p.match_id} p={p} />)}
        </>
      )}
      {obsolete.length > 0 && (
        <>
          <div style={{ color: '#444', fontSize: 11, letterSpacing: 1, margin: '20px 0 10px' }}>OBSOLETAS — filtros actuales ({obsolete.length})</div>
          {obsolete.map(p => <PredictionCard key={p.match_id} p={p} />)}
        </>
      )}
    </div>
  )
}

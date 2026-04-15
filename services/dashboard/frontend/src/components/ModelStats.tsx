import { useApi } from '../hooks/useApi'

interface Stats {
  accuracy_global: number
  accuracy_by_league: Record<string, number>
  weights: Record<string, number>
  weights_history: Array<{ week: string; weights: Record<string, number> }>
  total_predictions: number
  correct_predictions: number
}

const LEAGUE_NAMES: Record<string, string> = {
  PL: '🏴󠁧󠁢󠁥󠁮󠁧󠁿 Premier League',
  PD: '🇪🇸 La Liga',
  BL1: '🇩🇪 Bundesliga',
  SA: '🇮🇹 Serie A',
  FL1: '🇫🇷 Ligue 1',
  NBA: '🏀 NBA',
  NFL: '🏈 NFL',
  MLB: '⚾ MLB',
  NHL: '🏒 NHL',
  UFC: '🥊 UFC',
}

const FACTOR_COLORS: Record<string, string> = {
  poisson: '#F7931A',
  elo: '#2196F3',
  form: '#9C27B0',
  h2h: '#00BCD4',
}

function AccuracyBar({ label, value, count }: { label: string; value: number; count?: number }) {
  const color = value >= 0.6 ? '#00C853' : value >= 0.5 ? '#F7931A' : '#FF5252'
  return (
    <div style={{ marginBottom: 10 }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 4 }}>
        <span style={{ fontSize: 12, color: '#ccc' }}>{LEAGUE_NAMES[label] ?? label}</span>
        <span style={{ fontSize: 12, color, fontWeight: 'bold' }}>
          {(value * 100).toFixed(1)}%{count !== undefined ? ` (${count})` : ''}
        </span>
      </div>
      <div style={{ background: '#333', borderRadius: 3, height: 6 }}>
        <div style={{ width: `${Math.min(value * 100, 100)}%`, background: color, height: 6, borderRadius: 3, transition: 'width 0.3s' }} />
      </div>
    </div>
  )
}

function WeightBar({ label, value }: { label: string; value: number }) {
  const color = FACTOR_COLORS[label] ?? '#888'
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 8 }}>
      <span style={{ width: 64, color: '#888', fontSize: 12, textTransform: 'capitalize', textAlign: 'right' }}>{label}</span>
      <div style={{ flex: 1, background: '#333', borderRadius: 3, height: 10, position: 'relative' }}>
        <div style={{ width: `${value * 100}%`, background: color, height: 10, borderRadius: 3, transition: 'width 0.3s' }} />
      </div>
      <span style={{ width: 44, textAlign: 'right', color, fontWeight: 'bold', fontSize: 13 }}>{(value * 100).toFixed(1)}%</span>
    </div>
  )
}

function WeightsHistoryTable({ history }: { history: Stats['weights_history'] }) {
  if (!history?.length) return null

  // Get all factor keys from first entry
  const factors = history[0]?.weights ? Object.keys(history[0].weights) : []
  const recent = history.slice(0, 8)

  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
        <thead>
          <tr style={{ borderBottom: '1px solid #222' }}>
            <th style={{ textAlign: 'left', padding: '6px 8px', color: '#555', fontWeight: 'normal' }}>Semana</th>
            {factors.map(f => (
              <th key={f} style={{ textAlign: 'right', padding: '6px 8px', color: FACTOR_COLORS[f] ?? '#555', fontWeight: 'normal', textTransform: 'capitalize' }}>
                {f}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {recent.map((row, i) => {
            const prevRow = recent[i + 1]
            return (
              <tr key={row.week} style={{ borderBottom: '1px solid #1a1a1a' }}>
                <td style={{ padding: '8px', color: '#888', whiteSpace: 'nowrap' }}>{row.week}</td>
                {factors.map(f => {
                  const val = row.weights[f] ?? 0
                  const prevVal = prevRow?.weights[f] ?? val
                  const delta = val - prevVal
                  const arrow = Math.abs(delta) < 0.005 ? '' : delta > 0 ? ' ↑' : ' ↓'
                  const arrowColor = delta > 0 ? '#00C853' : delta < 0 ? '#FF5252' : ''
                  return (
                    <td key={f} style={{ padding: '8px', textAlign: 'right' }}>
                      <span style={{ color: '#ccc' }}>{(val * 100).toFixed(1)}%</span>
                      {arrow && <span style={{ color: arrowColor, fontSize: 10 }}>{arrow}</span>}
                    </td>
                  )
                })}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

export default function ModelStats() {
  const { data, loading, error } = useApi<Stats>('/api/stats')

  if (loading) return <p style={{ color: '#888', padding: 24 }}>Cargando estadísticas...</p>
  if (error) return <p style={{ color: '#F7931A', padding: 24 }}>Error: {error}</p>
  if (!data) return <p style={{ color: '#888', padding: 24 }}>Sin datos disponibles.</p>

  const accuracyColor = data.accuracy_global >= 0.6 ? '#00C853' : data.accuracy_global >= 0.5 ? '#F7931A' : '#FF5252'
  const leagueEntries = Object.entries(data.accuracy_by_league ?? {}).sort((a, b) => b[1] - a[1])
  const weightEntries = Object.entries(data.weights ?? {}).sort((a, b) => b[1] - a[1])

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 20 }}>
        <h2 style={{ color: '#F7931A', margin: 0 }}>📊 Estadísticas del Modelo</h2>
      </div>

      {/* KPI row */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: 12, marginBottom: 28 }}>
        <div style={{ background: '#141414', border: '1px solid #2a2a2a', borderRadius: 8, padding: '16px 20px' }}>
          <div style={{ color: '#555', fontSize: 11, marginBottom: 6, letterSpacing: 0.5 }}>ACCURACY GLOBAL</div>
          <div style={{ color: accuracyColor, fontSize: 36, fontWeight: 'bold', lineHeight: 1 }}>
            {(data.accuracy_global * 100).toFixed(1)}%
          </div>
        </div>
        <div style={{ background: '#141414', border: '1px solid #2a2a2a', borderRadius: 8, padding: '16px 20px' }}>
          <div style={{ color: '#555', fontSize: 11, marginBottom: 6, letterSpacing: 0.5 }}>PREDICCIONES</div>
          <div style={{ fontSize: 28, fontWeight: 'bold', lineHeight: 1 }}>
            <span style={{ color: '#00C853' }}>{data.correct_predictions}</span>
            <span style={{ color: '#444' }}>/{data.total_predictions}</span>
          </div>
          <div style={{ color: '#555', fontSize: 11, marginTop: 4 }}>correctas / total</div>
        </div>
        <div style={{ background: '#141414', border: '1px solid #2a2a2a', borderRadius: 8, padding: '16px 20px' }}>
          <div style={{ color: '#555', fontSize: 11, marginBottom: 6, letterSpacing: 0.5 }}>LIGAS ACTIVAS</div>
          <div style={{ color: '#F7931A', fontSize: 36, fontWeight: 'bold', lineHeight: 1 }}>
            {leagueEntries.length}
          </div>
        </div>
        <div style={{ background: '#141414', border: '1px solid #2a2a2a', borderRadius: 8, padding: '16px 20px' }}>
          <div style={{ color: '#555', fontSize: 11, marginBottom: 6, letterSpacing: 0.5 }}>SEMANAS HISTORIAL</div>
          <div style={{ color: '#ccc', fontSize: 36, fontWeight: 'bold', lineHeight: 1 }}>
            {data.weights_history?.length ?? 0}
          </div>
        </div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20, marginBottom: 28 }}>
        {/* Accuracy by league */}
        {leagueEntries.length > 0 && (
          <div style={{ background: '#141414', border: '1px solid #2a2a2a', borderRadius: 8, padding: '16px 20px' }}>
            <div style={{ color: '#444', fontSize: 11, marginBottom: 16, letterSpacing: 0.5 }}>ACCURACY POR LIGA</div>
            {leagueEntries.map(([league, acc]) => (
              <AccuracyBar key={league} label={league} value={acc} />
            ))}
          </div>
        )}

        {/* Current weights */}
        {weightEntries.length > 0 && (
          <div style={{ background: '#141414', border: '1px solid #2a2a2a', borderRadius: 8, padding: '16px 20px' }}>
            <div style={{ color: '#444', fontSize: 11, marginBottom: 16, letterSpacing: 0.5 }}>PESOS ACTUALES DEL MODELO</div>
            {weightEntries.map(([factor, weight]) => (
              <WeightBar key={factor} label={factor} value={weight} />
            ))}
            <div style={{ marginTop: 16, paddingTop: 12, borderTop: '1px solid #222', color: '#555', fontSize: 11 }}>
              Los pesos se ajustan automáticamente cada semana con ALETHEIA según los resultados.
            </div>
          </div>
        )}
      </div>

      {/* Weights history */}
      {data.weights_history?.length > 1 && (
        <div style={{ background: '#141414', border: '1px solid #2a2a2a', borderRadius: 8, padding: '16px 20px' }}>
          <div style={{ color: '#444', fontSize: 11, marginBottom: 16, letterSpacing: 0.5 }}>EVOLUCIÓN DE PESOS (últimas 8 semanas)</div>
          <WeightsHistoryTable history={data.weights_history} />
          <div style={{ marginTop: 12, color: '#444', fontSize: 11 }}>
            ↑ subió · ↓ bajó respecto a la semana anterior
          </div>
        </div>
      )}
    </div>
  )
}

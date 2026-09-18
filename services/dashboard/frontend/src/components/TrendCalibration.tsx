import { useApi } from '../hooks/useApi'

interface MarketCalibration {
  market: string
  pattern_type: 'series' | 'rolling' | 'model' | null
  sample_n: number
  hit_rate_fixed: number | null
  threshold_fixed: number
  threshold_effective: number
  target_hit_rate: number
  status: 'fixed' | 'at_fixed' | 'calibrated' | 'below_target'
  calibrated_n?: number
  calibrated_hit_rate?: number
  updated_at?: string
}

interface TrendAccuracy {
  total_graded: number
  calibration: MarketCalibration[]
}

const MARKET_LABEL: Record<string, string> = {
  team_goals_over: 'Goles equipo',
  btts: 'Ambos marcan',
  handicap: 'Hándicap',
  corners: 'Córners',
  cards: 'Tarjetas',
  red_cards: 'Expulsiones',
  shots: 'Tiros',
  shots_on_target: 'Tiros a puerta',
  fouls: 'Faltas',
  ht_goals: 'Goles 1ª parte',
  double_chance: 'Doble oportunidad',
  dnb: 'Sin empate (DNB)',
  exact_total: 'Total exacto',
  win_margin: 'Margen de victoria',
}

const PATTERN_TAG: Record<string, string> = { series: '🎯 series', rolling: '📉 rolling', model: '📐 model' }

// El umbral fijo de "corners/shots/..." es un ratio (>1.30), el de
// "team_goals_over/btts/handicap" un hit-rate (0.70), y el "model" una
// probabilidad o un ratio de dominancia — formatos distintos, no se pueden
// mostrar todos como "%". Los ratio-type se muestran como "1.30x".
const RATIO_MARKETS = new Set(['corners', 'cards', 'red_cards', 'shots', 'shots_on_target', 'fouls', 'ht_goals', 'exact_total', 'win_margin'])

function formatThreshold(market: string, value: number): string {
  return RATIO_MARKETS.has(market) ? `${value.toFixed(2)}x` : `${(value * 100).toFixed(0)}%`
}

function StatusBadge({ status }: { status: MarketCalibration['status'] }) {
  const map: Record<MarketCalibration['status'], { label: string; color: string; bg: string }> = {
    fixed: { label: '🔒 Fijo', color: '#888', bg: '#1e1e1e' },
    at_fixed: { label: '🎯 Calibrado', color: '#00C853', bg: '#0d2416' },
    calibrated: { label: '🎯 Calibrado', color: '#00C853', bg: '#0d2416' },
    below_target: { label: '⚠️ No alcanza objetivo', color: '#FF5252', bg: '#2a1414' },
  }
  const s = map[status]
  return (
    <span style={{ color: s.color, background: s.bg, borderRadius: 4, padding: '3px 8px', fontSize: 11, fontWeight: 'bold', whiteSpace: 'nowrap' }}>
      {s.label}
    </span>
  )
}

export default function TrendCalibration() {
  const { data, loading, error } = useApi<TrendAccuracy>('/api/trend-accuracy')

  if (loading) return <p style={{ color: '#888', padding: 24 }}>Cargando calibración...</p>
  if (error) return <p style={{ color: '#F7931A', padding: 24 }}>Error: {error}</p>
  if (!data || !data.calibration?.length) return <p style={{ color: '#888', padding: 24 }}>Sin mercados de tendencias todavía.</p>

  const rows = data.calibration
  const targetPct = (rows[0]?.target_hit_rate ?? 0.70) * 100
  const nCalibrated = rows.filter(r => r.status === 'calibrated' || r.status === 'at_fixed').length
  const nBelowTarget = rows.filter(r => r.status === 'below_target').length

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 20 }}>
        <h2 style={{ color: '#F7931A', margin: 0 }}>🧠 Auto-calibración de Tendencias</h2>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(160px, 1fr))', gap: 12, marginBottom: 20 }}>
        <div style={{ background: '#141414', border: '1px solid #2a2a2a', borderRadius: 8, padding: '16px 20px' }}>
          <div style={{ color: '#555', fontSize: 11, marginBottom: 6, letterSpacing: 0.5 }}>OBJETIVO DE PRECISIÓN</div>
          <div style={{ color: '#F7931A', fontSize: 32, fontWeight: 'bold', lineHeight: 1 }}>{targetPct.toFixed(0)}%</div>
          <div style={{ color: '#555', fontSize: 11, marginTop: 4 }}>mismo para los 14 mercados</div>
        </div>
        <div style={{ background: '#141414', border: '1px solid #2a2a2a', borderRadius: 8, padding: '16px 20px' }}>
          <div style={{ color: '#555', fontSize: 11, marginBottom: 6, letterSpacing: 0.5 }}>CALIBRADOS</div>
          <div style={{ color: '#00C853', fontSize: 32, fontWeight: 'bold', lineHeight: 1 }}>{nCalibrated}/{rows.length}</div>
        </div>
        {nBelowTarget > 0 && (
          <div style={{ background: '#141414', border: '1px solid #2a2a2a', borderRadius: 8, padding: '16px 20px' }}>
            <div style={{ color: '#555', fontSize: 11, marginBottom: 6, letterSpacing: 0.5 }}>NO ALCANZAN OBJETIVO</div>
            <div style={{ color: '#FF5252', fontSize: 32, fontWeight: 'bold', lineHeight: 1 }}>{nBelowTarget}</div>
          </div>
        )}
      </div>

      <div style={{ background: '#141414', border: '1px solid #2a2a2a', borderRadius: 8, padding: '16px 20px', overflowX: 'auto' }}>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12, minWidth: 720 }}>
          <thead>
            <tr style={{ borderBottom: '1px solid #222' }}>
              <th style={{ textAlign: 'left', padding: '6px 8px', color: '#555', fontWeight: 'normal' }}>Mercado</th>
              <th style={{ textAlign: 'left', padding: '6px 8px', color: '#555', fontWeight: 'normal' }}>Evidencia</th>
              <th style={{ textAlign: 'right', padding: '6px 8px', color: '#555', fontWeight: 'normal' }}>Graduadas</th>
              <th style={{ textAlign: 'right', padding: '6px 8px', color: '#555', fontWeight: 'normal' }}>Hit-rate</th>
              <th style={{ textAlign: 'right', padding: '6px 8px', color: '#555', fontWeight: 'normal' }}>Umbral fijo</th>
              <th style={{ textAlign: 'right', padding: '6px 8px', color: '#555', fontWeight: 'normal' }}>Umbral actual</th>
              <th style={{ textAlign: 'right', padding: '6px 8px', color: '#555', fontWeight: 'normal' }}>Estado</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => {
              const hitRateColor = r.hit_rate_fixed == null ? '#555'
                : r.hit_rate_fixed >= r.target_hit_rate ? '#00C853'
                : r.hit_rate_fixed >= r.target_hit_rate - 0.10 ? '#F7931A' : '#FF5252'
              const tightened = r.status === 'calibrated'
              return (
                <tr key={r.market} style={{ borderBottom: '1px solid #1a1a1a' }}>
                  <td style={{ padding: '8px', color: '#fff' }}>{MARKET_LABEL[r.market] ?? r.market}</td>
                  <td style={{ padding: '8px', color: '#888' }}>{r.pattern_type ? PATTERN_TAG[r.pattern_type] : '—'}</td>
                  <td style={{ padding: '8px', textAlign: 'right', color: '#ccc' }}>
                    {r.status === 'fixed' ? `${r.sample_n}/30` : r.sample_n}
                  </td>
                  <td style={{ padding: '8px', textAlign: 'right', color: hitRateColor, fontWeight: 'bold' }}>
                    {r.hit_rate_fixed != null ? `${(r.hit_rate_fixed * 100).toFixed(0)}%` : '—'}
                  </td>
                  <td style={{ padding: '8px', textAlign: 'right', color: '#888' }}>
                    {formatThreshold(r.market, r.threshold_fixed)}
                  </td>
                  <td style={{ padding: '8px', textAlign: 'right', color: tightened ? '#F7931A' : '#888', fontWeight: tightened ? 'bold' : 'normal' }}>
                    {formatThreshold(r.market, r.threshold_effective)}
                    {tightened && <span style={{ color: '#555', fontSize: 10 }}> ↑</span>}
                  </td>
                  <td style={{ padding: '8px', textAlign: 'right' }}><StatusBadge status={r.status} /></td>
                </tr>
              )
            })}
          </tbody>
        </table>

        <div style={{ marginTop: 16, paddingTop: 12, borderTop: '1px solid #222', color: '#555', fontSize: 11 }}>
          🔒 Fijo: menos de 30 señales graduadas, usa el umbral genérico de siempre · 🎯 Calibrado: ≥30 graduadas,
          umbral propio del mercado (se aprieta si el fijo no llega al {targetPct.toFixed(0)}%, nunca se afloja todavía) ·
          ⚠️ No alcanza objetivo: ni la mejor franja del mercado llega al {targetPct.toFixed(0)}% — se mantiene el umbral fijo.
        </div>
      </div>
    </div>
  )
}

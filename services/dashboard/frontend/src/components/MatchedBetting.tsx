import { useState, useEffect, useCallback } from 'react'

// ─── Types ────────────────────────────────────────────────────────────────────

interface CalcResult {
  type: string
  lay_stake: number
  liability: number
  profit_back: number
  profit_lay: number
  rating: number
  steps: string[]
}

interface MatchedSignal {
  signal_id: string
  signal_type: string          // "surebet" | "coverage"
  sport_key: string
  event_id: string
  commence_time: string
  home_team: string
  away_team: string
  selection: string
  back_bookmaker: string
  back_odds: number
  lay_bookmaker: string
  lay_odds: number
  confidence: string           // "high" | "medium" | "unknown"
  lay_age_seconds: number
  qualifying_rating: number
  freebet_snr_rating: number
  lay_stake_per_100: number
  liability_per_100: number
  profit_per_100: number
}

interface MatchedSignalsResp {
  signals: MatchedSignal[]
  count: number
  surebets: number
  coverage: number
  warning?: string
  error?: string
  fetched_at: string
}

interface BonusPlay {
  event: string
  selection: string
  sport_key: string
  commence_time: string
  ref_back_bookmaker: string
  ref_back_odds: number
  lay_bookmaker: string
  lay_odds: number
  estimated_benefit: number
  benefit_label: string
}

interface Bonus {
  id: string
  bookmaker: string
  title: string
  type: string
  amount: number
  min_odds: number
  requirement: string
  active: boolean
  verify?: boolean
  play: BonusPlay | null
}

interface BonusesResp {
  bonuses: Bonus[]
  count: number
  note: string
  fetched_at: string
}

interface Bet {
  id: string
  bet_type: string
  event: string
  back_stake: number
  back_odds: number
  lay_odds: number
  commission: number
  lay_stake: number
  liability: number
  profit_back: number
  profit_lay: number
  rating: number
  status: string
  pnl: number
  created_at: string
}

// ─── Shared helpers ────────────────────────────────────────────────────────────

const inputStyle: React.CSSProperties = {
  display: 'block', width: '100%', marginTop: 4, padding: '8px 10px',
  background: '#111', color: '#FFF', border: '1px solid #333',
  borderRadius: 4, boxSizing: 'border-box', fontSize: 14,
}

const labelStyle: React.CSSProperties = {
  display: 'block', color: '#888', fontSize: 12, marginBottom: 4,
}

const btnPrimary: React.CSSProperties = {
  background: '#F7931A', color: '#000', border: 'none', borderRadius: 6,
  padding: '10px 20px', cursor: 'pointer', fontWeight: 'bold', fontSize: 14,
}

const card: React.CSSProperties = {
  background: '#141414', border: '1px solid #2a2a2a',
  borderRadius: 8, padding: '16px 20px', marginBottom: 12,
}

function StatBox({ label, value, color = '#fff' }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ background: '#1e1e1e', border: '1px solid #2e2e2e', borderRadius: 6, padding: '6px 12px', minWidth: 80 }}>
      <div style={{ color: '#555', fontSize: 10, marginBottom: 2 }}>{label}</div>
      <div style={{ color, fontWeight: 'bold', fontSize: 14 }}>{value}</div>
    </div>
  )
}

// ─── Tab: Calculadora ──────────────────────────────────────────────────────────

function TabCalculadora({ onBetSaved }: { onBetSaved: () => void }) {
  const [form, setForm] = useState({
    type: 'qualifying',
    back_stake: '',
    back_odds: '',
    lay_odds: '',
    commission: '0.05',
  })
  const [result, setResult] = useState<CalcResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [event, setEvent] = useState('')

  const handleCalc = async () => {
    setLoading(true); setError(null)
    try {
      const res = await fetch('/api/calc', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          type: form.type,
          back_stake: parseFloat(form.back_stake),
          back_odds: parseFloat(form.back_odds),
          lay_odds: parseFloat(form.lay_odds),
          commission: parseFloat(form.commission),
        }),
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        throw new Error(d.detail || `HTTP ${res.status}`)
      }
      setResult(await res.json())
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Error desconocido')
    } finally { setLoading(false) }
  }

  const handleSave = async () => {
    if (!result) return
    setSaving(true)
    try {
      const res = await fetch('/api/save-bet', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          bet_type: form.type,
          event: event || 'Sin nombre',
          back_stake: parseFloat(form.back_stake),
          back_odds: parseFloat(form.back_odds),
          lay_odds: parseFloat(form.lay_odds),
          commission: parseFloat(form.commission),
          lay_stake: result.lay_stake,
          liability: result.liability,
          profit_back: result.profit_back,
          profit_lay: result.profit_lay,
          rating: result.rating,
          status: 'pendiente',
        }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      onBetSaved()
      setResult(null)
      setEvent('')
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Error guardando')
    } finally { setSaving(false) }
  }

  const typeLabels: Record<string, string> = {
    qualifying: 'Qualifying Bet',
    free_bet_snr: 'Free Bet SNR',
    free_bet_sr: 'Free Bet SR',
  }

  const fields: Array<{ key: keyof typeof form; label: string; step: string }> = [
    { key: 'back_stake', label: 'Stake Back (€)', step: '0.5' },
    { key: 'back_odds', label: 'Cuota Back', step: '0.01' },
    { key: 'lay_odds', label: 'Cuota Lay', step: '0.01' },
    { key: 'commission', label: 'Comisión Exchange (decimal)', step: '0.01' },
  ]

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 20 }}>
      {/* Form */}
      <div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <div>
            <label style={labelStyle}>Tipo de apuesta</label>
            <select
              value={form.type}
              onChange={e => setForm({ ...form, type: e.target.value })}
              style={inputStyle}
            >
              <option value="qualifying">Qualifying Bet</option>
              <option value="free_bet_snr">Free Bet SNR</option>
              <option value="free_bet_sr">Free Bet SR</option>
            </select>
          </div>
          <div>
            <label style={labelStyle}>Evento (opcional)</label>
            <input
              type="text"
              placeholder="ej: Real Madrid vs Barcelona"
              value={event}
              onChange={e => setEvent(e.target.value)}
              style={inputStyle}
            />
          </div>
          {fields.map(({ key, label, step }) => (
            <div key={key}>
              <label style={labelStyle}>{label}</label>
              <input
                type="number"
                step={step}
                value={form[key]}
                onChange={e => setForm({ ...form, [key]: e.target.value })}
                style={inputStyle}
              />
            </div>
          ))}
          <button onClick={handleCalc} disabled={loading} style={btnPrimary}>
            {loading ? 'Calculando...' : 'Calcular'}
          </button>
        </div>
        {error && <p style={{ color: '#FF5252', marginTop: 12, fontSize: 13 }}>Error: {error}</p>}
      </div>

      {/* Result */}
      <div>
        {result ? (
          <div style={{ ...card, height: '100%' }}>
            <div style={{ color: '#F7931A', fontWeight: 'bold', fontSize: 13, marginBottom: 12 }}>
              {typeLabels[result.type] || result.type}
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 16 }}>
              <StatBox label="LAY STAKE" value={`€${result.lay_stake.toFixed(2)}`} color="#fff" />
              <StatBox label="RESPONSABILIDAD" value={`€${result.liability.toFixed(2)}`} color="#FF5252" />
              <StatBox label="RATING" value={`${result.rating.toFixed(1)}%`} color={result.rating > 0 ? '#00C853' : '#FF5252'} />
              <StatBox label="P&L BACK" value={`€${result.profit_back.toFixed(2)}`} color={result.profit_back > 0 ? '#00C853' : '#FF5252'} />
              <StatBox label="P&L LAY" value={`€${result.profit_lay.toFixed(2)}`} color={result.profit_lay > 0 ? '#00C853' : '#FF5252'} />
            </div>
            <div style={{ borderTop: '1px solid #222', paddingTop: 12, marginBottom: 12 }}>
              <div style={{ color: '#444', fontSize: 11, marginBottom: 8, letterSpacing: 0.5 }}>PASOS</div>
              {result.steps.map((s, i) => (
                <div key={i} style={{ display: 'flex', gap: 8, marginBottom: 6, fontSize: 13, color: '#aaa' }}>
                  <span style={{ color: '#F7931A', fontWeight: 'bold', flexShrink: 0 }}>{i + 1}.</span>
                  <span>{s}</span>
                </div>
              ))}
            </div>
            <button onClick={handleSave} disabled={saving} style={{ ...btnPrimary, width: '100%', background: '#1e3a1e', color: '#00C853', border: '1px solid #00C853' }}>
              {saving ? 'Guardando...' : '+ Guardar en tracker'}
            </button>
          </div>
        ) : (
          <div style={{ ...card, display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#444', fontSize: 13, minHeight: 200 }}>
            Los resultados aparecerán aquí
          </div>
        )}
      </div>
    </div>
  )
}

// ─── Tab: Señales (detector back/lay real) ──────────────────────────────────────

function fmtKickoff(iso: string): string {
  if (!iso) return '—'
  const d = new Date(iso)
  if (isNaN(d.getTime())) return iso
  return d.toLocaleDateString('es-ES', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' })
}

const CONF_META: Record<string, { label: string; color: string }> = {
  high: { label: '● Fiable', color: '#00C853' },
  medium: { label: '● Media', color: '#F7931A' },
  unknown: { label: '● Sin dato', color: '#888' },
}

function SignalCard({ s }: { s: MatchedSignal }) {
  const isSure = s.signal_type === 'surebet'
  const accent = isSure ? '#00C853' : '#F7931A'
  const label = isSure ? 'SUREBET' : 'COBERTURA'
  const ratingColor = s.qualifying_rating >= 0 ? '#00C853' : '#FF5252'
  const conf = CONF_META[s.confidence] ?? CONF_META.unknown
  const ageMin = s.lay_age_seconds >= 0 ? `lay ${Math.round(s.lay_age_seconds / 60)} min` : 'lay s/fecha'
  return (
    <div style={{ ...card, borderLeft: `3px solid ${accent}` }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 10 }}>
        <div>
          <div style={{ fontWeight: 'bold', fontSize: 14 }}>{s.home_team} <span style={{ color: '#555' }}>v</span> {s.away_team}</div>
          <div style={{ color: '#555', fontSize: 12, marginTop: 2 }}>
            {s.sport_key} · {fmtKickoff(s.commence_time)}
          </div>
        </div>
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 4, flexShrink: 0 }}>
          <span style={{ background: accent + '22', color: accent, border: `1px solid ${accent}44`, borderRadius: 4, padding: '2px 8px', fontSize: 11, fontWeight: 'bold' }}>
            {label}
          </span>
          <span title={ageMin} style={{ color: conf.color, fontSize: 11 }}>{conf.label} · {ageMin}</span>
        </div>
      </div>

      <div style={{ color: '#ccc', fontSize: 13, marginBottom: 10 }}>
        Apostar a: <span style={{ color: '#fff', fontWeight: 'bold' }}>{s.selection}</span>
      </div>

      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        <StatBox label={`BACK · ${s.back_bookmaker}`} value={s.back_odds.toFixed(2)} color="#00C853" />
        <StatBox label={`LAY · ${s.lay_bookmaker}`} value={s.lay_odds.toFixed(2)} color="#F7931A" />
        <StatBox label="RATING" value={`${s.qualifying_rating.toFixed(2)}%`} color={ratingColor} />
        <StatBox label="BENEF./100€" value={`€${s.profit_per_100.toFixed(2)}`} color={ratingColor} />
        <StatBox label="LAY STAKE/100€" value={`€${s.lay_stake_per_100.toFixed(2)}`} />
        <StatBox label="RESPONSAB./100€" value={`€${s.liability_per_100.toFixed(2)}`} color="#FF5252" />
        <StatBox label="EV FREE BET SNR" value={`${s.freebet_snr_rating.toFixed(0)}%`} color="#9C27B0" />
      </div>
    </div>
  )
}

function TabSenales() {
  const [filter, setFilter] = useState<'all' | 'surebet' | 'coverage'>('all')
  const [data, setData] = useState<MatchedSignalsResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const q = filter === 'all' ? '' : `?signal_type=${filter}`
      const res = await fetch(`/api/matched-signals${q}`)
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setData(await res.json())
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Error cargando señales')
    } finally { setLoading(false) }
  }, [filter])

  useEffect(() => { load() }, [load])

  const signals = data?.signals ?? []
  const filters: Array<{ id: 'all' | 'surebet' | 'coverage'; label: string }> = [
    { id: 'all', label: `Todas (${data?.count ?? 0})` },
    { id: 'surebet', label: `Surebets (${data?.surebets ?? 0})` },
    { id: 'coverage', label: `Coberturas (${data?.coverage ?? 0})` },
  ]

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16, flexWrap: 'wrap', gap: 10 }}>
        <div style={{ display: 'flex', gap: 6 }}>
          {filters.map(f => (
            <button
              key={f.id}
              onClick={() => setFilter(f.id)}
              style={{
                background: filter === f.id ? '#F7931A' : 'transparent',
                color: filter === f.id ? '#000' : '#888',
                border: `1px solid ${filter === f.id ? '#F7931A' : '#333'}`,
                borderRadius: 6, padding: '5px 12px', cursor: 'pointer',
                fontSize: 12, fontWeight: filter === f.id ? 'bold' : 'normal',
              }}
            >
              {f.label}
            </button>
          ))}
        </div>
        <button onClick={load} disabled={loading} style={{ ...btnPrimary, padding: '6px 14px', fontSize: 12 }}>
          {loading ? '...' : '↻ Actualizar'}
        </button>
      </div>

      <div style={{ background: '#F7931A11', border: '1px solid #F7931A33', borderRadius: 6, padding: '8px 12px', marginBottom: 16, color: '#F7931A', fontSize: 12 }}>
        ⚠️ {data?.warning ?? 'Lay de Betfair sin liquidez/size — verifica el importe disponible antes de apostar.'}
      </div>

      {error && <p style={{ color: '#FF5252', fontSize: 13 }}>Error: {error}</p>}
      {loading && <p style={{ color: '#888', fontSize: 13 }}>Cargando señales...</p>}

      {!loading && signals.length === 0 && (
        <p style={{ color: '#666', fontSize: 13, textAlign: 'center', padding: '40px 0' }}>
          No hay señales vigentes. El escáner corre a diario; vuelve tras el próximo ciclo.
        </p>
      )}

      {signals.map(s => <SignalCard key={s.signal_id} s={s} />)}
    </div>
  )
}

// ─── Tab: Bonos (reales, con jugada desde señales) ──────────────────────────────

function TabBonos() {
  const [data, setData] = useState<BonusesResp | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const res = await fetch('/api/matched-bonuses')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setData(await res.json())
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Error cargando bonos')
    } finally { setLoading(false) }
  }, [])

  useEffect(() => { load() }, [load])

  const bonuses = data?.bonuses ?? []

  return (
    <div>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
        <span style={{ color: '#888', fontSize: 13 }}>{bonuses.length} bonos</span>
        <button onClick={load} disabled={loading} style={{ ...btnPrimary, padding: '6px 14px', fontSize: 12 }}>
          {loading ? '...' : '↻ Actualizar'}
        </button>
      </div>

      <div style={{ background: '#F7931A11', border: '1px solid #F7931A33', borderRadius: 6, padding: '8px 12px', marginBottom: 16, color: '#F7931A', fontSize: 12 }}>
        ℹ️ {data?.note ?? 'Bonos mantenidos manualmente; la jugada usa una señal detectada como referencia.'}
      </div>

      {error && <p style={{ color: '#FF5252', fontSize: 13 }}>Error: {error}</p>}
      {loading && <p style={{ color: '#888', fontSize: 13 }}>Cargando bonos...</p>}

      {!loading && bonuses.length === 0 && (
        <p style={{ color: '#666', fontSize: 13, textAlign: 'center', padding: '40px 0' }}>
          No hay bonos configurados.
        </p>
      )}

      {bonuses.map(b => {
        const p = b.play
        return (
          <div key={b.id} style={{ ...card, borderLeft: '3px solid #9C27B0' }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 8 }}>
              <div>
                <span style={{ fontWeight: 'bold', fontSize: 15 }}>{b.bookmaker}</span>
                <span style={{ marginLeft: 8, color: '#888', fontSize: 13 }}>{b.title}</span>
              </div>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexShrink: 0 }}>
                <span style={{ background: '#9C27B022', color: '#9C27B0', border: '1px solid #9C27B044', borderRadius: 4, padding: '2px 8px', fontSize: 11 }}>{b.type}</span>
                {b.verify && (
                  <span style={{ background: '#F7931A22', color: '#F7931A', border: '1px solid #F7931A44', borderRadius: 4, padding: '2px 8px', fontSize: 11 }}>verificar importe</span>
                )}
              </div>
            </div>

            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
              <StatBox label="IMPORTE" value={`€${b.amount}`} />
              <StatBox label="CUOTA MÍN." value={b.min_odds.toFixed(2)} />
            </div>

            {b.requirement && (
              <div style={{ color: '#666', fontSize: 12, marginBottom: 10 }}>📋 {b.requirement}</div>
            )}

            {p ? (
              <div style={{ background: '#12121a', border: '1px solid #2a2a3a', borderRadius: 6, padding: '10px 12px' }}>
                <div style={{ color: '#9C27B0', fontSize: 11, fontWeight: 'bold', marginBottom: 6 }}>JUGADA RECOMENDADA</div>
                <div style={{ fontSize: 13, color: '#ddd', marginBottom: 4 }}>
                  {p.event} — <span style={{ color: '#fff', fontWeight: 'bold' }}>{p.selection}</span>
                  <span style={{ color: '#555', fontSize: 11 }}> · {p.sport_key} · {fmtKickoff(p.commence_time)}</span>
                </div>
                <div style={{ fontSize: 12, color: '#aaa', marginBottom: 6 }}>
                  Back en <b>{b.bookmaker}</b> (ref. {p.ref_back_bookmaker} @{p.ref_back_odds?.toFixed(2)}) ·
                  Lay Betfair @{p.lay_odds?.toFixed(2)}
                </div>
                <StatBox label={p.benefit_label.toUpperCase()} value={`€${p.estimated_benefit.toFixed(2)}`}
                  color={p.estimated_benefit >= 0 ? '#00C853' : '#FF5252'} />
              </div>
            ) : (
              <div style={{ color: '#666', fontSize: 12, fontStyle: 'italic' }}>
                Sin señal vigente que encaje con este bono ahora mismo.
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ─── Tab: Tracker P&L ─────────────────────────────────────────────────────────

const STATUS_LABELS: Record<string, { label: string; color: string }> = {
  pendiente: { label: 'Pendiente', color: '#F7931A' },
  ganado_back: { label: 'Ganado (Back)', color: '#00C853' },
  ganado_lay: { label: 'Ganado (Lay)', color: '#00C853' },
  cancelado: { label: 'Cancelado', color: '#888' },
}

function TabTracker({ refreshKey }: { refreshKey: number }) {
  const [bets, setBets] = useState<Bet[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [updating, setUpdating] = useState<string | null>(null)

  const loadBets = useCallback(async () => {
    setLoading(true)
    try {
      const res = await fetch('/api/bets')
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setBets(await res.json())
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Error cargando apuestas')
    } finally { setLoading(false) }
  }, [])

  useEffect(() => { loadBets() }, [loadBets, refreshKey])

  const updateStatus = async (id: string, status: string) => {
    setUpdating(id)
    try {
      const res = await fetch(`/api/bets/${id}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ status }),
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      await loadBets()
    } catch (err: unknown) {
      console.error(err)
    } finally { setUpdating(null) }
  }

  if (loading) return <p style={{ color: '#888' }}>Cargando apuestas...</p>
  if (error) return <p style={{ color: '#FF5252' }}>Error: {error}</p>

  const totalPnl = bets.reduce((sum, b) => sum + (b.pnl ?? 0), 0)
  const resolved = bets.filter(b => b.status !== 'pendiente' && b.status !== 'cancelado')
  const winRate = resolved.length > 0 ? (resolved.filter(b => b.pnl > 0).length / resolved.length) * 100 : 0

  return (
    <div>
      {/* Summary */}
      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap', marginBottom: 20 }}>
        {[
          { label: 'P&L TOTAL', value: `${totalPnl >= 0 ? '+' : ''}€${totalPnl.toFixed(2)}`, color: totalPnl >= 0 ? '#00C853' : '#FF5252' },
          { label: 'APUESTAS', value: `${bets.length}`, color: '#fff' },
          { label: 'PENDIENTES', value: `${bets.filter(b => b.status === 'pendiente').length}`, color: '#F7931A' },
          { label: 'WIN RATE', value: resolved.length > 0 ? `${winRate.toFixed(0)}%` : '—', color: '#ccc' },
        ].map(({ label, value, color }) => (
          <div key={label} style={{ background: '#1e1e1e', border: '1px solid #2e2e2e', borderRadius: 8, padding: '12px 16px', flex: 1, minWidth: 100 }}>
            <div style={{ color: '#555', fontSize: 11, marginBottom: 4 }}>{label}</div>
            <div style={{ color, fontWeight: 'bold', fontSize: 20 }}>{value}</div>
          </div>
        ))}
      </div>

      {bets.length === 0 && (
        <p style={{ color: '#666', fontSize: 13, textAlign: 'center', padding: '40px 0' }}>
          No hay apuestas registradas. Usa la calculadora y guarda una apuesta.
        </p>
      )}

      {bets.map(b => {
        const st = STATUS_LABELS[b.status] ?? { label: b.status, color: '#888' }
        const dateStr = b.created_at ? new Date(b.created_at).toLocaleDateString('es-ES', { day: '2-digit', month: 'short', hour: '2-digit', minute: '2-digit' }) : '—'
        const betTypeLabels: Record<string, string> = { qualifying: 'Qualifying', free_bet_snr: 'Free SNR', free_bet_sr: 'Free SR' }

        return (
          <div key={b.id} style={{ ...card, borderLeft: `3px solid ${st.color}` }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 10 }}>
              <div>
                <div style={{ fontWeight: 'bold', fontSize: 14 }}>{b.event}</div>
                <div style={{ color: '#555', fontSize: 12, marginTop: 2 }}>
                  {betTypeLabels[b.bet_type] ?? b.bet_type} · {dateStr}
                </div>
              </div>
              <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-end', gap: 4 }}>
                <span style={{ background: st.color + '22', color: st.color, border: `1px solid ${st.color}44`, borderRadius: 4, padding: '2px 8px', fontSize: 11, fontWeight: 'bold' }}>
                  {st.label}
                </span>
                {b.pnl !== 0 && (
                  <span style={{ color: b.pnl > 0 ? '#00C853' : '#FF5252', fontWeight: 'bold', fontSize: 15 }}>
                    {b.pnl > 0 ? '+' : ''}€{b.pnl.toFixed(2)}
                  </span>
                )}
              </div>
            </div>

            <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap', marginBottom: 10 }}>
              <StatBox label="STAKE" value={`€${b.back_stake?.toFixed(2)}`} />
              <StatBox label="CUOTA BACK" value={b.back_odds?.toFixed(2)} />
              <StatBox label="CUOTA LAY" value={b.lay_odds?.toFixed(2)} />
              <StatBox label="LAY STAKE" value={`€${b.lay_stake?.toFixed(2)}`} />
              <StatBox label="RATING" value={`${b.rating?.toFixed(1)}%`} color={b.rating > 0 ? '#00C853' : '#FF5252'} />
            </div>

            {b.status === 'pendiente' && (
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {[
                  { status: 'ganado_back', label: '✓ Ganado (Back)', bg: '#1e3a1e', color: '#00C853', border: '#00C853' },
                  { status: 'ganado_lay', label: '✓ Ganado (Lay)', bg: '#1e2e3a', color: '#2196F3', border: '#2196F3' },
                  { status: 'cancelado', label: '✗ Cancelado', bg: '#2a2a2a', color: '#888', border: '#555' },
                ].map(({ status, label, bg, color, border }) => (
                  <button
                    key={status}
                    onClick={() => updateStatus(b.id, status)}
                    disabled={updating === b.id}
                    style={{
                      background: bg, color, border: `1px solid ${border}`,
                      borderRadius: 4, padding: '5px 12px', cursor: 'pointer',
                      fontSize: 12, fontWeight: 'bold',
                      opacity: updating === b.id ? 0.5 : 1,
                    }}
                  >
                    {updating === b.id ? '...' : label}
                  </button>
                ))}
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

// ─── Main component ────────────────────────────────────────────────────────────

const TABS = [
  { id: 'calc', label: '🧮 Calculadora' },
  { id: 'signals', label: '🎯 Señales' },
  { id: 'bonos', label: '🎁 Bonos' },
  { id: 'tracker', label: '📈 Tracker P&L' },
]

export default function MatchedBetting() {
  const [activeTab, setActiveTab] = useState('calc')
  const [trackerRefresh, setTrackerRefresh] = useState(0)

  const handleBetSaved = () => {
    setTrackerRefresh(n => n + 1)
    setActiveTab('tracker')
  }

  return (
    <div>
      <h2 style={{ color: '#F7931A', margin: '0 0 20px' }}>💰 Matched Betting</h2>

      {/* Tab bar */}
      <div style={{ display: 'flex', gap: 2, marginBottom: 24, borderBottom: '1px solid #222', paddingBottom: 0 }}>
        {TABS.map(tab => (
          <button
            key={tab.id}
            onClick={() => setActiveTab(tab.id)}
            style={{
              background: activeTab === tab.id ? '#F7931A' : 'transparent',
              color: activeTab === tab.id ? '#000' : '#888',
              border: 'none',
              borderBottom: activeTab === tab.id ? '2px solid #F7931A' : '2px solid transparent',
              borderRadius: '4px 4px 0 0',
              padding: '8px 16px',
              cursor: 'pointer',
              fontWeight: activeTab === tab.id ? 'bold' : 'normal',
              fontSize: 13,
              transition: 'all 0.15s',
            }}
          >
            {tab.label}
          </button>
        ))}
      </div>

      {activeTab === 'calc' && <TabCalculadora onBetSaved={handleBetSaved} />}
      {activeTab === 'signals' && <TabSenales />}
      {activeTab === 'bonos' && <TabBonos />}
      {activeTab === 'tracker' && <TabTracker refreshKey={trackerRefresh} />}
    </div>
  )
}

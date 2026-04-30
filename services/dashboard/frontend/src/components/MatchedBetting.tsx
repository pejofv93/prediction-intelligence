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

interface OddsEntry {
  bookmaker: string
  home: number
  draw?: number
  away: number
  is_exchange?: boolean
}

interface OddsResult {
  event: string
  odds: OddsEntry[]
  best_back: { bookmaker: string; selection?: string; odds: number } | null
  best_lay: { bookmaker: string; odds: number } | null
  warning: string
  fetched_at: string
}

interface Offer {
  bookmaker: string
  bonus: string
  amount: number
  type: string
  requirement: string
  rating: number
  status: string
  advice: string
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

// ─── Tab: Buscar cuotas ────────────────────────────────────────────────────────

function TabBuscarCuotas() {
  const [eventName, setEventName] = useState('')
  const [result, setResult] = useState<OddsResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleSearch = async () => {
    if (!eventName.trim()) return
    setLoading(true); setError(null)
    try {
      const res = await fetch('/api/find-odds', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ event: eventName }),
      })
      if (!res.ok) {
        const d = await res.json().catch(() => ({}))
        throw new Error(d.detail || `HTTP ${res.status}`)
      }
      setResult(await res.json())
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Error buscando cuotas')
    } finally { setLoading(false) }
  }

  return (
    <div>
      <div style={{ display: 'flex', gap: 10, marginBottom: 20 }}>
        <input
          type="text"
          placeholder="ej: Real Madrid vs Barcelona"
          value={eventName}
          onChange={e => setEventName(e.target.value)}
          onKeyDown={e => e.key === 'Enter' && handleSearch()}
          style={{ ...inputStyle, flex: 1, marginTop: 0 }}
        />
        <button onClick={handleSearch} disabled={loading} style={{ ...btnPrimary, flexShrink: 0 }}>
          {loading ? 'Buscando...' : '🔍 Buscar'}
        </button>
      </div>

      {error && <p style={{ color: '#FF5252', fontSize: 13 }}>Error: {error}</p>}

      {result && (
        <div>
          {result.warning && (
            <div style={{ background: '#F7931A11', border: '1px solid #F7931A33', borderRadius: 6, padding: '8px 12px', marginBottom: 16, color: '#F7931A', fontSize: 12 }}>
              ⚠️ {result.warning}
            </div>
          )}

          {/* Best picks */}
          {(result.best_back || result.best_lay) && (
            <div style={{ display: 'flex', gap: 12, marginBottom: 16, flexWrap: 'wrap' }}>
              {result.best_back && (
                <div style={{ ...card, flex: 1, minWidth: 160, borderLeft: '3px solid #00C853', marginBottom: 0 }}>
                  <div style={{ color: '#555', fontSize: 11, marginBottom: 4 }}>MEJOR BACK{result.best_back.selection ? ` — ${result.best_back.selection}` : ''}</div>
                  <div style={{ fontWeight: 'bold', color: '#00C853', fontSize: 20 }}>{result.best_back.odds}</div>
                  <div style={{ color: '#888', fontSize: 12 }}>{result.best_back.bookmaker}</div>
                </div>
              )}
              {result.best_lay && (
                <div style={{ ...card, flex: 1, minWidth: 160, borderLeft: '3px solid #F7931A', marginBottom: 0 }}>
                  <div style={{ color: '#555', fontSize: 11, marginBottom: 4 }}>MEJOR LAY (Exchange)</div>
                  <div style={{ fontWeight: 'bold', color: '#F7931A', fontSize: 20 }}>{result.best_lay.odds}</div>
                  <div style={{ color: '#888', fontSize: 12 }}>{result.best_lay.bookmaker}</div>
                </div>
              )}
            </div>
          )}

          {/* Odds table */}
          {result.odds.length > 0 && (() => {
            const isExchange = (o: OddsEntry) => o.is_exchange === true || o.bookmaker.toLowerCase().includes('betfair')
            const sorted = [...result.odds].sort((a, b) => (isExchange(b) ? 1 : 0) - (isExchange(a) ? 1 : 0))
            return (
              <div style={card}>
                <div style={{ color: '#444', fontSize: 11, marginBottom: 12, letterSpacing: 0.5 }}>CASAS ESPAÑOLAS</div>
                <div style={{ overflowX: 'auto' }}>
                  <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 13 }}>
                    <thead>
                      <tr style={{ borderBottom: '1px solid #222' }}>
                        {['Casa', 'Local', 'Empate', 'Visitante'].map(h => (
                          <th key={h} style={{ textAlign: 'left', padding: '4px 8px', color: '#555', fontWeight: 'normal' }}>{h}</th>
                        ))}
                      </tr>
                    </thead>
                    <tbody>
                      {sorted.map((o, i) => {
                        const exchange = isExchange(o)
                        const rowBg = exchange ? '#1a1208' : 'transparent'
                        return (
                          <tr key={i} style={{ borderBottom: '1px solid #1a1a1a', background: rowBg }}>
                            <td style={{ padding: '8px' }}>
                              <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
                                <span style={{ color: exchange ? '#F7931A' : '#ccc', fontWeight: 'bold' }}>{o.bookmaker}</span>
                                <span style={{ background: '#0d1f3c', color: '#5b9bd5', border: '1px solid #1a3a6e', borderRadius: 3, padding: '1px 5px', fontSize: 10, lineHeight: 1.4 }}>🇪🇸 España</span>
                                {exchange && (
                                  <span style={{ background: '#F7931A22', color: '#F7931A', border: '1px solid #F7931A44', borderRadius: 3, padding: '1px 5px', fontSize: 10, lineHeight: 1.4 }}>EXCHANGE (LAY)</span>
                                )}
                              </div>
                            </td>
                            <td style={{ padding: '8px', color: '#fff' }}>{o.home?.toFixed(2) ?? '—'}</td>
                            <td style={{ padding: '8px', color: '#888' }}>{o.draw?.toFixed(2) ?? '—'}</td>
                            <td style={{ padding: '8px', color: '#fff' }}>{o.away?.toFixed(2) ?? '—'}</td>
                          </tr>
                        )
                      })}
                    </tbody>
                  </table>
                </div>
              </div>
            )
          })()}
        </div>
      )}
    </div>
  )
}

// ─── Tab: Ofertas ──────────────────────────────────────────────────────────────

const ratingColors = ['#FF5252', '#FF5252', '#F7931A', '#F7931A', '#00C853', '#00C853']

function TabOfertas() {
  const [offers, setOffers] = useState<Offer[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(false)

  const fetchOffers = async () => {
    setLoading(true); setError(null)
    try {
      const res = await fetch('/api/fetch-offers', { method: 'POST' })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setOffers(await res.json())
      setLoaded(true)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Error cargando ofertas')
    } finally { setLoading(false) }
  }

  const typeColors: Record<string, string> = {
    welcome: '#9C27B0',
    reload: '#2196F3',
    cashback: '#00BCD4',
    free_bet: '#F7931A',
  }

  return (
    <div>
      {!loaded && (
        <div style={{ textAlign: 'center', padding: '40px 0' }}>
          <p style={{ color: '#666', marginBottom: 16, fontSize: 14 }}>
            Busca las mejores ofertas y bonos de casas de apuestas españolas en tiempo real via IA.
          </p>
          <button onClick={fetchOffers} disabled={loading} style={{ ...btnPrimary, padding: '12px 32px', fontSize: 15 }}>
            {loading ? 'Buscando ofertas...' : '🎁 Cargar ofertas actuales'}
          </button>
        </div>
      )}

      {loaded && (
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 16 }}>
          <span style={{ color: '#888', fontSize: 13 }}>{offers.length} ofertas encontradas</span>
          <button onClick={fetchOffers} disabled={loading} style={{ ...btnPrimary, padding: '6px 14px', fontSize: 12 }}>
            {loading ? '...' : '↻ Actualizar'}
          </button>
        </div>
      )}

      {error && <p style={{ color: '#FF5252', fontSize: 13 }}>Error: {error}</p>}

      {offers.map((o, i) => {
        const typeColor = typeColors[o.type] ?? '#888'
        const rc = ratingColors[Math.min(Math.max(Math.round(o.rating ?? 0), 0), 5)]
        return (
          <div key={i} style={{ ...card, borderLeft: `3px solid ${typeColor}` }}>
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', marginBottom: 8 }}>
              <div>
                <span style={{ fontWeight: 'bold', fontSize: 15 }}>{o.bookmaker}</span>
                <span style={{ marginLeft: 8, color: '#888', fontSize: 13 }}>{o.bonus}</span>
              </div>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center', flexShrink: 0 }}>
                <span style={{ background: typeColor + '22', color: typeColor, border: `1px solid ${typeColor}44`, borderRadius: 4, padding: '2px 8px', fontSize: 11 }}>
                  {o.type}
                </span>
                {o.status === 'activo' && (
                  <span style={{ background: '#00C85322', color: '#00C853', border: '1px solid #00C85344', borderRadius: 4, padding: '2px 8px', fontSize: 11 }}>
                    activo
                  </span>
                )}
              </div>
            </div>

            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 10 }}>
              <StatBox label="IMPORTE" value={`€${o.amount}`} color="#fff" />
              <div style={{ background: '#1e1e1e', border: '1px solid #2e2e2e', borderRadius: 6, padding: '6px 12px' }}>
                <div style={{ color: '#555', fontSize: 10, marginBottom: 2 }}>RATING</div>
                <div style={{ display: 'flex', gap: 2 }}>
                  {[1, 2, 3, 4, 5].map(n => (
                    <div key={n} style={{ width: 8, height: 8, borderRadius: 2, background: n <= (o.rating ?? 0) ? rc : '#333' }} />
                  ))}
                </div>
              </div>
            </div>

            {o.requirement && (
              <div style={{ color: '#666', fontSize: 12, marginBottom: 6 }}>
                📋 {o.requirement}
              </div>
            )}
            {o.advice && (
              <div style={{ color: '#F7931A', fontSize: 12 }}>
                💡 {o.advice}
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
  { id: 'odds', label: '📊 Buscar cuotas' },
  { id: 'offers', label: '🎁 Ofertas' },
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
      {activeTab === 'odds' && <TabBuscarCuotas />}
      {activeTab === 'offers' && <TabOfertas />}
      {activeTab === 'tracker' && <TabTracker refreshKey={trackerRefresh} />}
    </div>
  )
}

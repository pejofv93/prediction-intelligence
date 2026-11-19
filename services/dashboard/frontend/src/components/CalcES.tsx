import { useEffect, useMemo, useState } from 'react'

// Calculadora entre casas con licencia en España (+ lay/back en betfair.es).
// Toda la matemática vive en el backend (POST /api/matched-calc → shared/matched_calc.py).

export interface EsBookmaker {
  slug: string
  name: string
  domain: string
  kind: string // "sportsbook" | "exchange"
  has_api: boolean
  verify: boolean
}

interface CalcLeg {
  outcome: string
  kind: string
  bookmaker_name: string
  odds: number
  stake: number
  liability?: number
  effective_odds?: number
}

interface CalcResp {
  legs: CalcLeg[]
  profit_by_outcome: Record<string, number>
  profit_min: number
  profit_max: number
  retention_pct: number
  outlay: number
  steps: string[]
}

const input: React.CSSProperties = {
  padding: '6px 8px', background: '#111', color: '#FFF', border: '1px solid #333',
  borderRadius: 4, fontSize: 13, boxSizing: 'border-box', width: '100%',
}
const lbl: React.CSSProperties = { display: 'block', color: '#888', fontSize: 12, marginBottom: 4 }
const card: React.CSSProperties = {
  background: '#141414', border: '1px solid #2a2a2a', borderRadius: 8, padding: '16px 20px', marginBottom: 12,
}
const grid: React.CSSProperties = {
  display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12,
}

export function useEsBookmakers() {
  const [books, setBooks] = useState<EsBookmaker[]>([])
  const [commission, setCommission] = useState(0.05)
  useEffect(() => {
    fetch('/api/es-bookmakers')
      .then(r => (r.ok ? r.json() : Promise.reject(r.status)))
      .then(d => { setBooks(d.bookmakers || []); setCommission(d.default_commission ?? 0.05) })
      .catch(() => setBooks([]))
  }, [])
  return { books, commission }
}

type CoverMode = 'dutch' | 'lay'

interface LegForm { kind: string; bookmaker: string; odds: string }
const EMPTY_LEG: LegForm = { kind: 'back', bookmaker: '', odds: '' }

export default function CalcES() {
  const { books, commission: defComm } = useEsBookmakers()
  const sportsbooks = books.filter(b => b.kind === 'sportsbook')

  const [nOut, setNOut] = useState<2 | 3>(3)
  const outcomes = nOut === 3 ? ['1', 'X', '2'] : ['1', '2']
  const [kind, setKind] = useState('qualifying')
  const [bonusBook, setBonusBook] = useState('')
  const [bonusOut, setBonusOut] = useState('1')
  const [stake, setStake] = useState('10')
  const [odds, setOdds] = useState('')
  const [refundAmount, setRefundAmount] = useState('10')
  const [refundValue, setRefundValue] = useState('0.7')
  const [mode, setMode] = useState<CoverMode>('dutch')
  const [layOdds, setLayOdds] = useState('')
  const [commission, setCommission] = useState('')
  const [legs, setLegs] = useState<Record<string, LegForm>>({})
  const [result, setResult] = useState<CalcResp | null>(null)
  const [error, setError] = useState<string | null>(null)

  const coverOutcomes = useMemo(
    () => outcomes.filter(o => o !== bonusOut),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [nOut, bonusOut],
  )
  const comm = commission === '' ? defComm : parseFloat(commission)

  const setLeg = (o: string, patch: Partial<LegForm>) =>
    setLegs(prev => ({ ...prev, [o]: { ...EMPTY_LEG, ...prev[o], ...patch } }))

  const submit = async () => {
    setError(null)
    setResult(null)
    const legsBody = mode === 'lay'
      ? [{ outcome: bonusOut, kind: 'lay', odds: parseFloat(layOdds), bookmaker: 'betfair_es_exchange', commission: comm }]
      : coverOutcomes.map(o => {
          const l = legs[o] || EMPTY_LEG
          return l.kind === 'exchange_back'
            ? { outcome: o, kind: 'exchange_back', odds: parseFloat(l.odds), bookmaker: 'betfair_es_exchange', commission: comm }
            : { outcome: o, kind: 'back', odds: parseFloat(l.odds), bookmaker: l.bookmaker }
        })
    try {
      const res = await fetch('/api/matched-calc', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          outcomes, bonus_kind: kind, bonus_outcome: bonusOut, bonus_bookmaker: bonusBook,
          stake: parseFloat(stake), odds: parseFloat(odds),
          refund_amount: parseFloat(refundAmount) || 0, refund_value: parseFloat(refundValue) || 0,
          legs: legsBody,
        }),
      })
      const data = await res.json()
      if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`)
      setResult(data)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div>
      <div style={{ ...card, borderColor: '#F7931A44' }}>
        <div style={{ color: '#888', fontSize: 12, marginBottom: 12 }}>
          Solo casas con licencia en España y el exchange de betfair.es. La cobertura nunca va en la casa del bono.
        </div>
        <div style={grid}>
          <label><span style={lbl}>Resultados</span>
            <select style={input} value={nOut} onChange={e => { setNOut(Number(e.target.value) as 2 | 3); setBonusOut('1') }}>
              <option value={3}>3 (1 X 2)</option><option value={2}>2 (1 2)</option>
            </select></label>
          <label><span style={lbl}>Tipo</span>
            <select style={input} value={kind} onChange={e => setKind(e.target.value)}>
              <option value="qualifying">Calificante</option>
              <option value="freebet_snr">Freebet SNR</option>
              <option value="freebet_sr">Freebet SR</option>
              <option value="refund">Reembolso si pierde</option>
            </select></label>
          <label><span style={lbl}>Casa del bono</span>
            <select style={input} value={bonusBook} onChange={e => setBonusBook(e.target.value)}>
              <option value="">—</option>
              {sportsbooks.map(b => <option key={b.slug} value={b.slug}>{b.name}</option>)}
            </select></label>
          <label><span style={lbl}>Resultado del bono</span>
            <select style={input} value={bonusOut} onChange={e => setBonusOut(e.target.value)}>
              {outcomes.map(o => <option key={o} value={o}>{o}</option>)}
            </select></label>
          <label><span style={lbl}>{kind.startsWith('freebet') ? 'Freebet (€)' : 'Stake (€)'}</span>
            <input style={input} type="number" value={stake} onChange={e => setStake(e.target.value)} /></label>
          <label><span style={lbl}>Cuota del bono</span>
            <input style={input} type="number" step="0.01" value={odds} onChange={e => setOdds(e.target.value)} /></label>
          {kind === 'refund' && <>
            <label><span style={lbl}>Reembolso R (€)</span>
              <input style={input} type="number" value={refundAmount} onChange={e => setRefundAmount(e.target.value)} /></label>
            <label><span style={lbl}>Valor k del reembolso (0-1)</span>
              <input style={input} type="number" step="0.05" value={refundValue} onChange={e => setRefundValue(e.target.value)} /></label>
          </>}
        </div>

        <div style={{ margin: '16px 0 8px', display: 'flex', gap: 16, fontSize: 13 }}>
          <label><input type="radio" checked={mode === 'dutch'} onChange={() => setMode('dutch')} /> Cubrir en otras casas / exchange</label>
          <label><input type="radio" checked={mode === 'lay'} onChange={() => setMode('lay')} /> Lay del bono en betfair.es</label>
        </div>

        {mode === 'lay' ? (
          <div style={grid}>
            <label><span style={lbl}>Cuota lay ('{bonusOut}')</span>
              <input style={input} type="number" step="0.01" value={layOdds} onChange={e => setLayOdds(e.target.value)} /></label>
            <label><span style={lbl}>Comisión ({(defComm * 100).toFixed(0)} % por defecto)</span>
              <input style={input} type="number" step="0.01" placeholder={String(defComm)} value={commission} onChange={e => setCommission(e.target.value)} /></label>
          </div>
        ) : (
          <div>
            {coverOutcomes.map(o => {
              const l = legs[o] || EMPTY_LEG
              return (
                <div key={o} style={{ ...grid, marginBottom: 8 }}>
                  <label><span style={lbl}>Cubrir '{o}' en</span>
                    <select style={input} value={l.kind} onChange={e => setLeg(o, { kind: e.target.value })}>
                      <option value="back">Casa ES</option>
                      <option value="exchange_back">Back en betfair.es</option>
                    </select></label>
                  {l.kind === 'back' && (
                    <label><span style={lbl}>Casa</span>
                      <select style={input} value={l.bookmaker} onChange={e => setLeg(o, { bookmaker: e.target.value })}>
                        <option value="">—</option>
                        {sportsbooks.filter(b => b.slug !== bonusBook).map(b => <option key={b.slug} value={b.slug}>{b.name}</option>)}
                      </select></label>
                  )}
                  <label><span style={lbl}>Cuota '{o}'</span>
                    <input style={input} type="number" step="0.01" value={l.odds} onChange={e => setLeg(o, { odds: e.target.value })} /></label>
                </div>
              )
            })}
            {coverOutcomes.some(o => legs[o]?.kind === 'exchange_back') && (
              <label style={{ display: 'block', maxWidth: 220 }}><span style={lbl}>Comisión betfair.es</span>
                <input style={input} type="number" step="0.01" placeholder={String(defComm)} value={commission} onChange={e => setCommission(e.target.value)} /></label>
            )}
          </div>
        )}

        <button onClick={submit} style={{ marginTop: 16, background: '#F7931A', color: '#000', border: 'none', borderRadius: 6, padding: '10px 20px', cursor: 'pointer', fontWeight: 'bold' }}>
          Calcular
        </button>
        {error && <div style={{ color: '#F44336', marginTop: 12, fontSize: 13 }}>⚠️ {error}</div>}
      </div>

      {result && (
        <div style={card}>
          <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', marginBottom: 12 }}>
            <div><div style={lbl}>Beneficio mínimo</div>
              <div style={{ fontSize: 20, color: result.profit_min >= 0 ? '#4CAF50' : '#F44336' }}>{result.profit_min.toFixed(2)} €</div></div>
            <div><div style={lbl}>Retención</div><div style={{ fontSize: 20 }}>{result.retention_pct.toFixed(2)} %</div></div>
            <div><div style={lbl}>Dinero a poner</div><div style={{ fontSize: 20 }}>{result.outlay.toFixed(2)} €</div></div>
          </div>
          <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
            <thead><tr style={{ color: '#888', textAlign: 'left' }}>
              <th>Resultado</th><th>Dónde</th><th>Tipo</th><th>Cuota</th><th>Stake</th><th>Resp.</th></tr></thead>
            <tbody>
              {result.legs.map((l, i) => (
                <tr key={i}>
                  <td>{l.outcome}</td><td>{l.bookmaker_name}</td><td>{l.kind}</td>
                  <td>{l.odds.toFixed(2)}{l.effective_odds ? ` (ef. ${l.effective_odds.toFixed(2)})` : ''}</td>
                  <td>{l.stake.toFixed(2)} €</td><td>{l.liability != null ? `${l.liability.toFixed(2)} €` : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ marginTop: 12, fontSize: 13 }}>
            Beneficio por resultado: {Object.entries(result.profit_by_outcome).map(([o, p]) => (
              <span key={o} style={{ marginRight: 12, color: p >= 0 ? '#4CAF50' : '#F44336' }}>{o}: {p.toFixed(2)} €</span>
            ))}
          </div>
          <ol style={{ marginTop: 12, color: '#ccc', fontSize: 13 }}>
            {result.steps.map((s, i) => <li key={i}>{s}</li>)}
          </ol>
          <div style={{ color: '#888', fontSize: 12 }}>Comprueba las cuotas y la liquidez del exchange antes de apostar.</div>
        </div>
      )}
    </div>
  )
}

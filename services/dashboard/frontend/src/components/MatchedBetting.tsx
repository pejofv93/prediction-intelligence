import { useState } from 'react'

interface CalcResult {
  lay_stake: number
  liability: number
  profit_back: number
  profit_lay: number
  rating: number
  steps: string[]
}

export default function MatchedBetting() {
  const [form, setForm] = useState({
    type: 'qualifying',
    back_stake: '',
    back_odds: '',
    lay_odds: '',
    commission: '0.05',
  })
  const [result, setResult] = useState<CalcResult | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const handleCalc = async () => {
    setLoading(true)
    setError(null)
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
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      setResult(await res.json())
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Error desconocido')
    } finally {
      setLoading(false)
    }
  }

  // TODO: implementar UI completa en Sesion 7
  return (
    <div>
      <h2 style={{ color: '#F7931A', marginBottom: 16 }}>🧮 Calculadora Matched Betting</h2>
      <div style={{ background: '#1A1A1A', borderRadius: 8, padding: 24, maxWidth: 480 }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          <label>
            Tipo
            <select
              value={form.type}
              onChange={e => setForm({ ...form, type: e.target.value })}
              style={{ display: 'block', width: '100%', marginTop: 4, padding: 8, background: '#111', color: '#FFF', border: '1px solid #333', borderRadius: 4 }}
            >
              <option value="qualifying">Qualifying Bet</option>
              <option value="free_bet_snr">Free Bet SNR</option>
              <option value="free_bet_sr">Free Bet SR</option>
            </select>
          </label>
          {['back_stake', 'back_odds', 'lay_odds', 'commission'].map(field => (
            <label key={field}>
              {field.replace('_', ' ')}
              <input
                type="number"
                step="0.01"
                value={form[field as keyof typeof form]}
                onChange={e => setForm({ ...form, [field]: e.target.value })}
                style={{ display: 'block', width: '100%', marginTop: 4, padding: 8, background: '#111', color: '#FFF', border: '1px solid #333', borderRadius: 4, boxSizing: 'border-box' }}
              />
            </label>
          ))}
          <button
            onClick={handleCalc}
            disabled={loading}
            style={{ background: '#F7931A', color: '#000', border: 'none', borderRadius: 6, padding: '10px 20px', cursor: 'pointer', fontWeight: 'bold' }}
          >
            {loading ? 'Calculando...' : 'Calcular'}
          </button>
        </div>
        {error && <p style={{ color: '#F7931A', marginTop: 12 }}>Error: {error}</p>}
        {result && (
          <div style={{ marginTop: 16, borderTop: '1px solid #333', paddingTop: 16 }}>
            <div>Lay Stake: <strong>€{result.lay_stake.toFixed(2)}</strong></div>
            <div>Liability: <strong>€{result.liability.toFixed(2)}</strong></div>
            <div>Rating: <strong>{result.rating.toFixed(1)}%</strong></div>
            <ul style={{ marginTop: 8, paddingLeft: 20, color: '#888', fontSize: 13 }}>
              {result.steps.map((s, i) => <li key={i}>{s}</li>)}
            </ul>
          </div>
        )}
      </div>
    </div>
  )
}

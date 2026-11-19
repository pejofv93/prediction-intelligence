import { useCallback, useEffect, useState } from 'react'

// Registro de bonos (entrada manual) + estado de cuenta por casa con licencia en España.
// API: /api/bonus-claims y /api/matched-bookmakers (services/dashboard/api/bonus_claims.py).

interface Leg {
  role: string
  kind: string
  bookmaker: string
  event: string
  selection: string
  odds: number
  stake: number
  result: string
  commission?: number
  pnl: number | null
}

interface Claim {
  id: string
  bookmaker: string
  title: string
  bonus_kind: string
  amount: number
  status: string
  legs: Leg[]
  notes: string
  pnl: number
  pending_legs: number
  expires_at?: string | null
  created_at: string
}

interface Book {
  slug: string
  name: string
  kind: string
  has_api: boolean
  account_status: string
  notes: string
}

const STATUSES = ['pendiente', 'calificante_colocada', 'esperando_freebet', 'freebet_disponible',
  'freebet_colocada', 'rollover_en_curso', 'completado', 'caducado', 'cancelado', 'limitado']
const ACCOUNT = ['sin_cuenta', 'activa', 'limitada', 'cerrada']

const input: React.CSSProperties = {
  padding: '6px 8px', background: '#111', color: '#FFF', border: '1px solid #333',
  borderRadius: 4, fontSize: 13,
}
const card: React.CSSProperties = {
  background: '#141414', border: '1px solid #2a2a2a', borderRadius: 8, padding: '12px 16px', marginBottom: 10,
}
const btn: React.CSSProperties = {
  background: '#F7931A', color: '#000', border: 'none', borderRadius: 6, padding: '6px 14px',
  cursor: 'pointer', fontWeight: 'bold', fontSize: 13,
}

async function api(url: string, method = 'GET', body?: unknown) {
  const res = await fetch(url, {
    method, headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  const data = await res.json().catch(() => ({}))
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`)
  return data
}

const EMPTY_LEG = { role: 'cobertura', kind: 'back', bookmaker: '', odds: '', stake: '', selection: '' }

function ClaimCard({ c, books, onChange, onError }: {
  c: Claim; books: Book[]; onChange: () => void; onError: (m: string) => void
}) {
  const [leg, setLeg] = useState(EMPTY_LEG)
  const name = (slug: string) => books.find(b => b.slug === slug)?.name || slug

  const patch = (body: unknown) => api(`/api/bonus-claims/${c.id}`, 'PATCH', body).then(onChange).catch(e => onError(e.message))
  const setResult = (i: number, result: string) =>
    patch({ legs: c.legs.map((l, j) => (j === i ? { ...l, result } : l)) })
  const addLeg = () => {
    patch({ legs: [...c.legs, { ...leg, odds: parseFloat(leg.odds), stake: parseFloat(leg.stake), event: c.title }] })
    setLeg(EMPTY_LEG)
  }

  return (
    <div style={card}>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
        <strong style={{ color: '#F7931A' }}>{name(c.bookmaker)}</strong>
        <span>{c.title}</span>
        <span style={{ color: '#888', fontSize: 12 }}>{c.bonus_kind} · {c.amount} €</span>
        <select style={input} value={c.status} onChange={e => patch({ status: e.target.value })}>
          {STATUSES.map(s => <option key={s} value={s}>{s}</option>)}
        </select>
        <span style={{ color: c.pnl >= 0 ? '#4CAF50' : '#F44336' }}>P&L {c.pnl.toFixed(2)} €</span>
        {c.pending_legs > 0 && <span style={{ color: '#888', fontSize: 12 }}>{c.pending_legs} patas pendientes</span>}
        <button style={{ ...btn, background: '#333', color: '#fff' }}
          onClick={() => { if (confirm('¿Borrar este bono?')) api(`/api/bonus-claims/${c.id}`, 'DELETE').then(onChange) }}>
          Borrar
        </button>
      </div>
      {c.legs.length > 0 && (
        <table style={{ width: '100%', fontSize: 12, marginTop: 8, borderCollapse: 'collapse' }}>
          <thead><tr style={{ color: '#888', textAlign: 'left' }}>
            <th>Rol</th><th>Tipo</th><th>Dónde</th><th>Selección</th><th>Cuota</th><th>Stake</th><th>Resultado</th><th>P&L</th></tr></thead>
          <tbody>
            {c.legs.map((l, i) => (
              <tr key={i}>
                <td>{l.role}</td><td>{l.kind}</td><td>{name(l.bookmaker)}</td><td>{l.selection}</td>
                <td>{l.odds.toFixed(2)}</td><td>{l.stake.toFixed(2)}</td>
                <td><select style={input} value={l.result} onChange={e => setResult(i, e.target.value)}>
                  {['pendiente', 'ganada', 'perdida', 'nula'].map(r => <option key={r}>{r}</option>)}
                </select></td>
                <td>{l.pnl == null ? '—' : l.pnl.toFixed(2)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <div style={{ display: 'flex', gap: 6, marginTop: 8, flexWrap: 'wrap' }}>
        <select style={input} value={leg.role} onChange={e => setLeg({ ...leg, role: e.target.value })}>
          {['calificante', 'freebet', 'cobertura', 'rollover'].map(r => <option key={r}>{r}</option>)}
        </select>
        <select style={input} value={leg.kind} onChange={e => setLeg({ ...leg, kind: e.target.value })}>
          {['back', 'freebet_snr', 'freebet_sr', 'exchange_back', 'lay'].map(k => <option key={k}>{k}</option>)}
        </select>
        {!['lay', 'exchange_back'].includes(leg.kind) && (
          <select style={input} value={leg.bookmaker} onChange={e => setLeg({ ...leg, bookmaker: e.target.value })}>
            <option value="">casa…</option>
            {books.filter(b => b.kind === 'sportsbook').map(b => <option key={b.slug} value={b.slug}>{b.name}</option>)}
          </select>
        )}
        <input style={{ ...input, width: 110 }} placeholder="selección" value={leg.selection} onChange={e => setLeg({ ...leg, selection: e.target.value })} />
        <input style={{ ...input, width: 70 }} placeholder="cuota" value={leg.odds} onChange={e => setLeg({ ...leg, odds: e.target.value })} />
        <input style={{ ...input, width: 70 }} placeholder="stake" value={leg.stake} onChange={e => setLeg({ ...leg, stake: e.target.value })} />
        <button style={btn} onClick={addLeg}>+ pata</button>
      </div>
    </div>
  )
}

export default function BonusClaims() {
  const [claims, setClaims] = useState<Claim[]>([])
  const [summary, setSummary] = useState({ pnl_total: 0, count: 0 })
  const [books, setBooks] = useState<Book[]>([])
  const [error, setError] = useState<string | null>(null)
  const [form, setForm] = useState({ bookmaker: '', title: '', bonus_kind: 'freebet_snr', amount: '' })

  const load = useCallback(() => {
    setError(null)
    api('/api/bonus-claims').then(d => { setClaims(d.claims); setSummary({ pnl_total: d.pnl_total, count: d.count }) })
      .catch(e => setError(e.message))
    api('/api/matched-bookmakers').then(d => setBooks(d.bookmakers)).catch(e => setError(e.message))
  }, [])
  useEffect(load, [load])

  const create = () =>
    api('/api/bonus-claims', 'POST', { ...form, amount: parseFloat(form.amount) || 0 })
      .then(() => { setForm({ ...form, title: '', amount: '' }); load() })
      .catch(e => setError(e.message))

  const setAccount = (slug: string, account_status: string, notes: string) =>
    api(`/api/matched-bookmakers/${slug}`, 'PUT', { account_status, notes }).then(load).catch(e => setError(e.message))

  return (
    <div>
      {error && <div style={{ color: '#F44336', marginBottom: 12 }}>⚠️ {error}</div>}
      <div style={{ ...card, borderColor: '#F7931A44' }}>
        <div style={{ marginBottom: 8 }}>
          {summary.count} bonos · P&L total <span style={{ color: summary.pnl_total >= 0 ? '#4CAF50' : '#F44336' }}>{summary.pnl_total.toFixed(2)} €</span>
        </div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          <select style={input} value={form.bookmaker} onChange={e => setForm({ ...form, bookmaker: e.target.value })}>
            <option value="">casa…</option>
            {books.filter(b => b.kind === 'sportsbook').map(b => <option key={b.slug} value={b.slug}>{b.name}</option>)}
          </select>
          <input style={input} placeholder="título" value={form.title} onChange={e => setForm({ ...form, title: e.target.value })} />
          <select style={input} value={form.bonus_kind} onChange={e => setForm({ ...form, bonus_kind: e.target.value })}>
            {['freebet_snr', 'freebet_sr', 'refund', 'deposit_bonus', 'otro'].map(k => <option key={k}>{k}</option>)}
          </select>
          <input style={{ ...input, width: 80 }} placeholder="importe €" value={form.amount} onChange={e => setForm({ ...form, amount: e.target.value })} />
          <button style={btn} onClick={create}>Añadir bono</button>
        </div>
      </div>

      {claims.map(c => <ClaimCard key={c.id} c={c} books={books} onChange={load} onError={setError} />)}

      <h3 style={{ color: '#F7931A', marginTop: 24 }}>Estado de cuenta por casa</h3>
      <table style={{ width: '100%', fontSize: 13, borderCollapse: 'collapse' }}>
        <thead><tr style={{ color: '#888', textAlign: 'left' }}><th>Casa</th><th>API gratis</th><th>Cuenta</th><th>Notas</th></tr></thead>
        <tbody>
          {books.filter(b => b.kind === 'sportsbook').map(b => (
            <tr key={b.slug}>
              <td>{b.name}</td><td>{b.has_api ? 'sí' : '—'}</td>
              <td><select style={input} value={b.account_status} onChange={e => setAccount(b.slug, e.target.value, b.notes)}>
                {ACCOUNT.map(a => <option key={a}>{a}</option>)}
              </select></td>
              <td><input style={input} defaultValue={b.notes} onBlur={e => e.target.value !== b.notes && setAccount(b.slug, b.account_status, e.target.value)} /></td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

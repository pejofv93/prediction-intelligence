import { useApi } from '../hooks/useApi'

interface Stats {
  accuracy_global: number
  accuracy_by_league: Record<string, number>
  weights: Record<string, number>
  weights_history: Array<{ week: string; weights: Record<string, number> }>
  total_predictions: number
  correct_predictions: number
}

export default function ModelStats() {
  const { data, loading, error } = useApi<Stats>('/api/stats')

  if (loading) return <p style={{ color: '#888' }}>Cargando estadísticas...</p>
  if (error) return <p style={{ color: '#F7931A' }}>Error: {error}</p>
  if (!data) return <p style={{ color: '#888' }}>Sin datos.</p>

  // TODO: implementar charts completos en Sesion 7
  return (
    <div>
      <h2 style={{ color: '#F7931A', marginBottom: 16 }}>📊 Estadísticas del Modelo</h2>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(200px, 1fr))', gap: 16 }}>
        <div style={{ background: '#1A1A1A', borderRadius: 8, padding: 16 }}>
          <div style={{ color: '#888', fontSize: 12 }}>Accuracy Global</div>
          <div style={{ color: '#F7931A', fontSize: 32, fontWeight: 'bold' }}>
            {(data.accuracy_global * 100).toFixed(1)}%
          </div>
        </div>
        <div style={{ background: '#1A1A1A', borderRadius: 8, padding: 16 }}>
          <div style={{ color: '#888', fontSize: 12 }}>Predicciones</div>
          <div style={{ color: '#F7931A', fontSize: 32, fontWeight: 'bold' }}>
            {data.correct_predictions}/{data.total_predictions}
          </div>
        </div>
      </div>

      <h3 style={{ color: '#FFF', marginTop: 24, marginBottom: 12 }}>Pesos del Modelo</h3>
      <div style={{ background: '#1A1A1A', borderRadius: 8, padding: 16 }}>
        {Object.entries(data.weights).map(([key, value]) => (
          <div key={key} style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 8 }}>
            <span style={{ width: 80, color: '#888' }}>{key}</span>
            <div style={{ flex: 1, background: '#333', borderRadius: 4, height: 8 }}>
              <div style={{ width: `${value * 100}%`, background: '#F7931A', height: 8, borderRadius: 4 }} />
            </div>
            <span style={{ width: 48, textAlign: 'right', color: '#F7931A' }}>{(value * 100).toFixed(1)}%</span>
          </div>
        ))}
      </div>
    </div>
  )
}

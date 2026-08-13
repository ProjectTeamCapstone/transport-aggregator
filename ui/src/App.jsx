import { useState } from 'react'
import SearchBar from './components/SearchBar'
import ResultsTabs from './components/ResultsTabs'
import { searchNatural, searchStructured } from './api'
import './styles.css'

export default function App() {
  const [results, setResults] = useState(null)
  const [understood, setUnderstood] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  const runSearch = async (request) => {
    setBusy(true); setError(null); setUnderstood(null)
    try {
      const data = request.kind === 'natural'
        ? await searchNatural(request.q)
        : await searchStructured(request)
      setResults(data)
      if (data.understood) setUnderstood({ ...data.understood, by: data.interpreted_by })
    } catch (err) {
      setError(err)
      setResults(null)
    } finally {
      setBusy(false)
    }
  }

  return (
    <main className="app">
      <header className="app__head">
        <h1>NaijaFares</h1>
        <p>Compare coaches and flights between Lagos, Abuja, Onitsha, Port Harcourt and London.</p>
      </header>

      <SearchBar onSearch={runSearch} busy={busy} />

      {/* Echo back what the system understood. A silent misreading is worse
          than a visible one - the user can correct what they can see. */}
      {understood && (
        <p className="understood" data-testid="understood">
          Showing <strong>{understood.origin} to {understood.destination}</strong>
          {understood.date ? ` on ${understood.date}` : ', all upcoming dates'},
          {' '}{understood.preference} first.
        </p>
      )}

      {error && (
        <div className="notice notice--error" data-testid="error">
          <p>{error.message}</p>
          {error.detail?.hint && <p className="notice__hint">{error.detail.hint}</p>}
        </div>
      )}

      <ResultsTabs results={results} initialTab={results?.show || 'cheapest'} />
    </main>
  )
}

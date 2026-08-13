import { useState } from 'react'

const PLACES = [
  { code: 'LOS', name: 'Lagos' },
  { code: 'ABV', name: 'Abuja' },
  { code: 'ONI', name: 'Onitsha' },
  { code: 'PHC', name: 'Port Harcourt' },
  { code: 'LHR', name: 'London' },
]

/**
 * Two ways to ask, one result. The plain-English box is primary because it is
 * how people actually think about a trip; the dropdowns stay because typing a
 * sentence is a poor way to change one field.
 */
export default function SearchBar({ onSearch, busy }) {
  const [mode, setMode] = useState('text')
  const [text, setText] = useState('')
  const [origin, setOrigin] = useState('LOS')
  const [dest, setDest] = useState('ABV')
  const [date, setDate] = useState('')

  const submit = (event) => {
    event.preventDefault()
    if (mode === 'text') {
      if (text.trim().length >= 2) onSearch({ kind: 'natural', q: text.trim() })
    } else {
      onSearch({ kind: 'structured', origin, dest, date })
    }
  }

  return (
    <form className="search" onSubmit={submit}>
      <div className="search__modes">
        <button type="button" className={`link ${mode === 'text' ? 'link--active' : ''}`}
                onClick={() => setMode('text')}>
          Describe your trip
        </button>
        <button type="button" className={`link ${mode === 'form' ? 'link--active' : ''}`}
                onClick={() => setMode('form')}>
          Use the form
        </button>
      </div>

      {mode === 'text' ? (
        <div className="search__row">
          <input
            className="search__text"
            aria-label="Describe your trip"
            placeholder="cheapest way to Abuja next Friday"
            value={text}
            onChange={(e) => setText(e.target.value)}
          />
          <button className="btn btn--primary" type="submit" disabled={busy || text.trim().length < 2}>
            {busy ? 'Searching…' : 'Search'}
          </button>
        </div>
      ) : (
        <div className="search__row">
          <label>From
            <select aria-label="From" value={origin} onChange={(e) => setOrigin(e.target.value)}>
              {PLACES.map((p) => <option key={p.code} value={p.code}>{p.name}</option>)}
            </select>
          </label>
          <label>To
            <select aria-label="To" value={dest} onChange={(e) => setDest(e.target.value)}>
              {PLACES.map((p) => <option key={p.code} value={p.code}>{p.name}</option>)}
            </select>
          </label>
          <label>Date
            <input aria-label="Date" type="date" value={date} onChange={(e) => setDate(e.target.value)} />
          </label>
          <button className="btn btn--primary" type="submit" disabled={busy}>
            {busy ? 'Searching…' : 'Search'}
          </button>
        </div>
      )}
    </form>
  )
}

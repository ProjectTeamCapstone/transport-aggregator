import React, { useState, useEffect } from 'react'
import { 
  searchNatural, 
  searchStructured, 
  predict, 
  bookOptions, 
  book 
} from './api'
import './styles.css'

const NGN = (n) => "₦" + Number(n || 0).toLocaleString("en-NG")
const DUR = (m) => `${Math.floor((m || 0) / 60)}h ${(m || 0) % 60}m`

const PLACES = [
  { code: "LOS", name: "Lagos" },
  { code: "ABV", name: "Abuja" },
  { code: "PHC", name: "Port Harcourt" },
  { code: "ONI", name: "Onitsha" },
  { code: "LHR", name: "London Heathrow" }
]

const TEAM_MEMBERS = [
  "Warieta Gift Ejovwoke",
  "Ndionu Nnamdi",
  "Ipadeola Ladipo",
  "Macaulay Emmanuel",
  "Maduechesi Chidiebere",
  "Lasisi Oluwadolapo",
  "Maduagwuna Onyedikachukwu"
]

export default function App() {
  const [activeTab, setActiveTab] = useState('home')

  const [cheapestResults, setCheapestResults] = useState([])
  const [fastestResults, setFastestResults] = useState([])
  const [displayMode, setDisplayMode] = useState('cheapest')
  const [predictions, setPredictions] = useState({})
  const [understood, setUnderstood] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const [hasSearched, setHasSearched] = useState(false)

  const [origin, setOrigin] = useState("LOS")
  const [destination, setDestination] = useState("ABV")
  const [date, setDate] = useState("")

  const [nlQuery, setNlQuery] = useState("")

  const [selectedOffer, setSelectedOffer] = useState(null)
  const [bookOption, setBookOption] = useState(null)
  const [passenger, setPassenger] = useState({ given_name: '', family_name: '', email: '' })
  const [bookingStatus, setBookingStatus] = useState(null)
  const [bookingBusy, setBookingBusy] = useState(false)

  // Check if passenger form is fully filled out
  const isPassengerFormValid = Boolean(
    passenger.given_name.trim() && 
    passenger.family_name.trim() && 
    passenger.email.trim()
  )

  useEffect(() => {
    setError(null)
    setUnderstood(null)
  }, [activeTab])

  const loadPredictions = async (offers) => {
    const predMap = {}
    await Promise.all(
      (offers || []).map(async (offer) => {
        try {
          const res = await predict(offer.offer_id)
          predMap[offer.offer_id] = res
        } catch (e) {
          predMap[offer.offer_id] = null
        }
      })
    )
    setPredictions((prev) => ({ ...prev, ...predMap }))
  }

  const executeSearch = async (request) => {
    if (request.kind === 'structured' && request.origin === request.destination) {
      setError({
        message: "Please choose different cities for your origin and destination.",
        detail: { hint: "Origin and destination cannot be the same city." }
      })
      setHasSearched(true)
      setCheapestResults([])
      setFastestResults([])
      return
    }

    setBusy(true)
    setError(null)
    setUnderstood(null)
    setHasSearched(true)
    setCheapestResults([])
    setFastestResults([])

    try {
      let data
      if (request.kind === 'natural') {
        data = await searchNatural(request.q)
      } else {
        data = await searchStructured(request)
      }

      const cheapest = Array.isArray(data?.cheapest) 
        ? data.cheapest 
        : (Array.isArray(data?.results) ? data.results : [])

      const fastest = Array.isArray(data?.fastest) 
        ? data.fastest 
        : (Array.isArray(data?.results) ? data.results : [])

      setCheapestResults(cheapest)
      setFastestResults(fastest)

      if (data?.show) {
        setDisplayMode(data.show)
      }

      if (data?.origin || data?.understood) {
        setUnderstood({
          origin: data?.understood?.origin || data?.origin,
          destination: data?.understood?.destination || data?.destination,
          date: data?.understood?.date || data?.date,
          preference: data?.understood?.preference || 'cheapest'
        })
      }

      const allOffers = [...cheapest, ...fastest]
      if (allOffers.length > 0) {
        loadPredictions(allOffers)
      }
    } catch (err) {
      setError({
        message: err?.message || "Unable to complete search at this time.",
        detail: err?.detail || { hint: "Please try again or select a supported travel route." }
      })
    } finally {
      setBusy(false)
    }
  }

  const handleFormSearch = (e) => {
    e?.preventDefault()
    executeSearch({ kind: 'structured', origin, destination, date })
  }

  const handleNlSearch = (queryText) => {
    const q = typeof queryText === 'string' ? queryText : nlQuery
    if (!q || !q.trim()) return
    executeSearch({ kind: 'natural', q: q.trim() })
  }

  const handleInitiateBooking = async (offer) => {
    setSelectedOffer(offer)
    setBookingStatus(null)
    setBookOption(null)
    // Reset passenger form on opening modal
    setPassenger({ given_name: '', family_name: '', email: '' })

    try {
      const options = await bookOptions(offer.offer_id)
      setBookOption(options)
    } catch (e) {
      setBookOption({
        books_via_api: false,
        provider: 'deeplink',
        reason: 'Carrier checkout handoff available.'
      })
    }
  }

  const handleConfirmBooking = async (e) => {
    e.preventDefault()
    if (!isPassengerFormValid) return

    setBookingBusy(true)
    try {
      const payload = {
        offer_id: selectedOffer.offer_id,
        quoted_price_ngn: selectedOffer.price_ngn,
        passenger: {
          given_name: passenger.given_name.trim(),
          family_name: passenger.family_name.trim(),
          email: passenger.email.trim()
        }
      }

      const res = await book(payload)
      setBookingStatus(res)

      if (res.handoff_url) {
        window.open(res.handoff_url, '_blank', 'noopener,noreferrer')
      }
    } catch (err) {
      setBookingStatus({
        state: 'failed',
        detail: err.message || 'We could not process this handoff request. Please try again.'
      })
    } finally {
      setBookingBusy(false)
    }
  }

  const currentResults = displayMode === 'cheapest' ? cheapestResults : fastestResults

  return (
    <main className="app">
      {/* Sticky Top Nav */}
      <nav className="top-nav">
        <div className="top-nav__container">
          <div className="top-nav__brand" onClick={() => setActiveTab('home')}>
            <span className="brand-icon">⚡</span>
            <span className="brand-name">NaijaFare</span>
          </div>

          <div className="top-nav__desktop-menu">
            <button 
              className={`nav-link ${activeTab === 'home' ? 'nav-link--active' : ''}`}
              onClick={() => setActiveTab('home')}
            >
              Home
            </button>
            <button 
              className={`nav-link ${activeTab === 'natural' ? 'nav-link--active' : ''}`}
              onClick={() => setActiveTab('natural')}
            >
              Description Search
            </button>
            <button 
              className={`nav-link ${activeTab === 'about' ? 'nav-link--active' : ''}`}
              onClick={() => setActiveTab('about')}
            >
              About
            </button>
            <button 
              className={`nav-link ${activeTab === 'team' ? 'nav-link--active' : ''}`}
              onClick={() => setActiveTab('team')}
            >
              Team
            </button>
          </div>

          <div className="top-nav__actions">
            <button className="btn btn--secondary" onClick={() => setActiveTab('natural')}>AI Search</button>
          </div>
        </div>
      </nav>

      {/* Hero Banner */}
      <header className="hero-banner">
        <div className="hero-banner__content">
          <span className="hero-subheading">NIGERIA'S TRAVEL FARE PREDICTION ENGINE</span>
          <h1 className="hero-title">Best Fares. Smart Predictions. Direct Booking.</h1>
          <p className="hero-description">
            Compare multi-modal bus & flight rates, foresee price hikes, and route directly to verified carrier sites.
          </p>

          <div className="hero-trust-badges">
            <span>📈 <strong>24h Fare</strong> Predictions</span>
            <span>🚌 <strong>Road & Air</strong> Fares</span>
            <span>🔗 <strong>Direct Carrier</strong> Redirects</span>
            <span>🛡️ <strong>Zero</strong> Extra Fees</span>
          </div>
        </div>
      </header>

      {/* VIEW 1: Home */}
      {activeTab === 'home' && (
        <section className="search-card">
          <form onSubmit={handleFormSearch}>
            <div className="search-card__row">
              <label htmlFor="origin">
                From
                <select id="origin" value={origin} onChange={(e) => setOrigin(e.target.value)}>
                  {PLACES.map((p) => (
                    <option key={p.code} value={p.code}>{p.name} ({p.code})</option>
                  ))}
                </select>
              </label>

              <label htmlFor="destination">
                To
                <select id="destination" value={destination} onChange={(e) => setDestination(e.target.value)}>
                  {PLACES.map((p) => (
                    <option key={p.code} value={p.code}>{p.name} ({p.code})</option>
                  ))}
                </select>
              </label>

              <label htmlFor="date">
                Departure Date
                <input type="date" id="date" value={date} onChange={(e) => setDate(e.target.value)} />
              </label>

              <button type="submit" className="btn btn--primary" disabled={busy}>
                {busy ? 'Searching...' : 'Compare Fares'}
              </button>
            </div>
          </form>
        </section>
      )}

      {/* VIEW 2: Description Search */}
      {activeTab === 'natural' && (
        <section className="search-card">
          <label className="card-label">Ask in plain English</label>
          <div className="search-card__row">
            <input 
              className="search-input-text"
              placeholder="e.g. cheapest way to Abuja next Friday" 
              value={nlQuery}
              onChange={(e) => setNlQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleNlSearch(nlQuery)}
            />
            <button className="btn btn--primary" onClick={() => handleNlSearch(nlQuery)} disabled={busy}>
              {busy ? 'Searching...' : 'Search'}
            </button>
          </div>

          <div style={{ marginTop: '20px', display: 'flex', gap: '10px', flexWrap: 'wrap' }}>
            <button 
              className="btn btn--ghost" 
              style={{ fontSize: '13px', padding: '8px 16px', height: 'auto' }}
              onClick={() => { setNlQuery('cheapest way to Abuja next Friday'); handleNlSearch('cheapest way to Abuja next Friday'); }}
            >
              cheapest way to Abuja next Friday
            </button>
            <button 
              className="btn btn--ghost" 
              style={{ fontSize: '13px', padding: '8px 16px', height: 'auto' }}
              onClick={() => { setNlQuery('fastest flight from Lagos to London'); handleNlSearch('fastest flight from Lagos to London'); }}
            >
              fastest flight to London
            </button>
            <button 
              className="btn btn--ghost" 
              style={{ fontSize: '13px', padding: '8px 16px', height: 'auto' }}
              onClick={() => { setNlQuery('cheapest bus Lagos to Onitsha'); handleNlSearch('cheapest bus Lagos to Onitsha'); }}
            >
              cheapest bus to Onitsha
            </button>
          </div>
        </section>
      )}

      {/* VIEW 3: About View */}
      {activeTab === 'about' && (
        <section className="search-card">
          <h3 style={{ marginTop: 0, color: 'var(--wakanow-blue)', fontSize: '1.4rem' }}>About NaijaFare</h3>
          <p style={{ margin: '14px 0 16px', color: 'var(--muted)', lineHeight: '1.7' }}>
            NaijaFare is an intelligent travel expense optimization and price prediction tool designed specifically for Nigerian transit routes.
          </p>
          <p style={{ margin: '14px 0 16px', color: 'var(--muted)', lineHeight: '1.7' }}>
            Rather than taking direct payments, NaijaFare analyzes price trends across road and air operators, forecasts whether prices will rise in the next 24 hours, and seamlessly redirects users to official carrier portals to finalize tickets securely.
          </p>

          <div className="notice notice--stale">
            <strong style={{ color: 'var(--ink)' }}>🎓 Educational Demo Notice</strong>
            <p className="notice__hint">
              This application was developed as a capstone demo project for Pan-Atlantic University (PAU).
            </p>
          </div>
        </section>
      )}

      {/* VIEW 4: Team View */}
      {activeTab === 'team' && (
        <section className="search-card">
          <h3 style={{ marginTop: 0, color: 'var(--wakanow-blue)', fontSize: '1.4rem' }}>Project Team</h3>
          <p style={{ margin: '8px 0 24px', color: 'var(--muted)' }}>
            PAU Data Science & Big Data Capstone Project — <strong>Group 3 Members</strong>
          </p>

          <div className="team-grid">
            {TEAM_MEMBERS.map((name, index) => {
              const initials = name
                .split(" ")
                .map(n => n[0])
                .join("")
                .slice(0, 2)

              return (
                <div className="team-card" key={index}>
                  <div className="team-card__avatar">{initials}</div>
                  <div className="team-card__info">
                    <h4>{name}</h4>
                    <p>Team Member</p>
                  </div>
                </div>
              )
            })}
          </div>
        </section>
      )}

      {/* Search Echo */}
      {activeTab !== 'about' && activeTab !== 'team' && understood && (
        <div className="understood" data-testid="understood">
          Showing <strong>{understood.origin || 'Search Results'} to {understood.destination || 'All'}</strong>
          {understood.date ? ` on ${understood.date}` : ', all upcoming dates'}
          {understood.preference ? `, ${understood.preference} first` : ''}.
        </div>
      )}

      {/* User-Friendly Errors */}
      {activeTab !== 'about' && activeTab !== 'team' && error && (
        <div className="notice notice--error" data-testid="error">
          <p style={{ margin: 0, fontWeight: 700 }}>{error.message}</p>
          {error.detail?.hint && <p className="notice__hint">{error.detail.hint}</p>}
        </div>
      )}

      {/* Loading Indicator */}
      {busy && (
        <div className="notice" style={{ textAlign: 'center', padding: '30px' }}>
          <strong>Comparing real-time fares across Nigerian routes...</strong>
        </div>
      )}

      {/* Empty Search Fallback */}
      {!busy && hasSearched && activeTab !== 'about' && activeTab !== 'team' && cheapestResults.length === 0 && fastestResults.length === 0 && !error && (
        <div className="notice">
          No fares match your search criteria. Try a different route/date or clear filters.
        </div>
      )}

      {/* Results List */}
      {!busy && activeTab !== 'about' && activeTab !== 'team' && (cheapestResults.length > 0 || fastestResults.length > 0) && (
        <div className="results__list">
          <div style={{ display: 'flex', gap: '12px', marginBottom: '16px' }}>
            <button 
              className={`btn ${displayMode === 'cheapest' ? 'btn--primary' : 'btn--ghost'}`}
              onClick={() => setDisplayMode('cheapest')}
            >
              Cheapest ({cheapestResults.length})
            </button>
            <button 
              className={`btn ${displayMode === 'fastest' ? 'btn--primary' : 'btn--ghost'}`}
              onClick={() => setDisplayMode('fastest')}
            >
              Fastest ({fastestResults.length})
            </button>
          </div>

          {currentResults.length === 0 ? (
            <div className="notice">
              No options found for this view filter.
            </div>
          ) : (
            currentResults.map((item, idx) => {
              const modeClass = item.mode === 'road' ? 'mode--road' : 'mode--air'
              const pred = predictions[item.offer_id]
              const probPct = pred ? Math.round(pred.probability * 100) : null
              const badgeClass = pred && pred.probability >= 0.6 ? 'badge--rise' : pred && pred.probability >= 0.4 ? 'badge--unsure' : 'badge--steady'

              const depDate = item.depart_time ? new Date(item.depart_time).toLocaleDateString() : 'N/A'
              const depTime = item.depart_time ? new Date(item.depart_time).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : ''

              return (
                <article className="offer" key={item.offer_id || idx}>
                  <div className="offer__head">
                    <h3>{item.carrier}</h3>
                    <span className={`mode ${modeClass}`}>{item.mode}</span>
                  </div>

                  <dl className="offer__facts">
                    <div>
                      <dt>Depart</dt>
                      <dd>{depDate} <small>({depTime})</small></dd>
                    </div>
                    <div>
                      <dt>Total Min (Door-to-Door)</dt>
                      <dd>{DUR(item.door_to_door_min || item.duration_min)}</dd>
                    </div>
                    <div>
                      <dt>Price</dt>
                      <dd className="offer__price">{NGN(item.price_ngn)}</dd>
                    </div>
                  </dl>

                  <div className="offer__actions">
                    {probPct !== null ? (
                      <span className={`badge ${badgeClass}`}>{probPct}% ↑ price rise 24h</span>
                    ) : (
                      <span className="badge badge--steady">Predicting...</span>
                    )}

                    <button 
                      className="btn btn--carrier-redirect" 
                      style={{ marginLeft: 'auto' }}
                      onClick={() => handleInitiateBooking(item)}
                    >
                      Continue on {item.carrier} ↗
                    </button>
                  </div>
                </article>
              )
            })
          )}
        </div>
      )}

      {/* Mobile Bottom Navigation */}
      <nav className="bottom-nav">
        <div className="bottom-nav__inner">
          <button 
            className={`bottom-nav__item ${activeTab === 'home' ? 'bottom-nav__item--active' : ''}`}
            onClick={() => setActiveTab('home')}
          >
            <div className="bottom-nav__icon-pill">
              <svg className="bottom-nav__icon" viewBox="0 0 24 24" fill="currentColor">
                <path d="M10 20v-6h4v6h5v-8h3L12 3 2 12h3v8z"/>
              </svg>
            </div>
            <span className="bottom-nav__label">Home</span>
          </button>

          <button 
            className={`bottom-nav__item bottom-nav__item--center ${activeTab === 'natural' ? 'bottom-nav__item--active-center' : ''}`}
            onClick={() => setActiveTab('natural')}
          >
            <div className="bottom-nav__center-btn">
              <svg className="bottom-nav__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5">
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"></path>
              </svg>
            </div>
            <span className="bottom-nav__label">AI Search</span>
          </button>

          <button 
            className={`bottom-nav__item ${activeTab === 'about' ? 'bottom-nav__item--active' : ''}`}
            onClick={() => setActiveTab('about')}
          >
            <div className="bottom-nav__icon-pill">
              <svg className="bottom-nav__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <circle cx="12" cy="12" r="10"></circle>
                <line x1="12" y1="16" x2="12" y2="12"></line>
                <line x1="12" y1="8" x2="12.01" y2="8"></line>
              </svg>
            </div>
            <span className="bottom-nav__label">About</span>
          </button>

          <button 
            className={`bottom-nav__item ${activeTab === 'team' ? 'bottom-nav__item--active' : ''}`}
            onClick={() => setActiveTab('team')}
          >
            <div className="bottom-nav__icon-pill">
              <svg className="bottom-nav__icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"></path>
                <circle cx="9" cy="7" r="4"></circle>
                <path d="M23 21v-2a4 4 0 0 0-3-3.87"></path>
                <path d="M16 3.13a4 4 0 0 1 0 7.75"></path>
              </svg>
            </div>
            <span className="bottom-nav__label">Team</span>
          </button>
        </div>
      </nav>

      {/* Booking Overlay Modal */}
      {selectedOffer && (
        <div className="modal-overlay" onClick={() => setSelectedOffer(null)}>
          <div className="booking-result" onClick={(e) => e.stopPropagation()}>
            <h3>{selectedOffer.carrier} Booking</h3>
            <p className="booking-result__prices">
              Quoted Price: <strong>{NGN(selectedOffer.price_ngn)}</strong>
            </p>

            {bookOption?.reason && (
              <p className="notice__hint" style={{ marginBottom: '16px' }}>
                {bookOption.reason}
              </p>
            )}

            {!bookingStatus ? (
              <form onSubmit={handleConfirmBooking} style={{ textAlign: 'left' }}>
                <label style={{ display: 'block', marginBottom: '8px', fontSize: '0.8rem', fontWeight: 700 }}>
                  Given Name *
                  <input 
                    type="text" 
                    required 
                    style={{ width: '100%', padding: '8px', marginTop: '4px' }}
                    value={passenger.given_name}
                    onChange={(e) => setPassenger({ ...passenger, given_name: e.target.value })}
                  />
                </label>

                <label style={{ display: 'block', marginBottom: '8px', fontSize: '0.8rem', fontWeight: 700 }}>
                  Family Name *
                  <input 
                    type="text" 
                    required 
                    style={{ width: '100%', padding: '8px', marginTop: '4px' }}
                    value={passenger.family_name}
                    onChange={(e) => setPassenger({ ...passenger, family_name: e.target.value })}
                  />
                </label>

                <label style={{ display: 'block', marginBottom: '16px', fontSize: '0.8rem', fontWeight: 700 }}>
                  Email Address *
                  <input 
                    type="email" 
                    required 
                    style={{ width: '100%', padding: '8px', marginTop: '4px' }}
                    value={passenger.email}
                    onChange={(e) => setPassenger({ ...passenger, email: e.target.value })}
                  />
                </label>

                <div style={{ display: 'flex', gap: '8px' }}>
                  <button 
                    type="submit" 
                    className="btn btn--primary" 
                    style={{ flex: 1 }}
                    disabled={bookingBusy || !isPassengerFormValid}
                  >
                    {bookingBusy ? 'Processing...' : (bookOption?.books_via_api ? 'Book Ticket' : 'Continue on Carrier Site')}
                  </button>
                  <button 
                    type="button" 
                    className="btn btn--ghost" 
                    onClick={() => setSelectedOffer(null)}
                  >
                    Cancel
                  </button>
                </div>
              </form>
            ) : (
              <div>
                <span className={`badge ${bookingStatus.state === 'confirmed' || bookingStatus.state === 'pending' ? 'badge--steady' : 'badge--rise'}`}>
                  {(bookingStatus.state || 'RESULT').toUpperCase()}
                </span>
                <p style={{ marginTop: '12px' }}>{bookingStatus.detail || 'Request completed.'}</p>

                <button 
                  className="btn btn--primary" 
                  style={{ marginTop: '16px', width: '100%' }} 
                  onClick={() => setSelectedOffer(null)}
                >
                  Done
                </button>
              </div>
            )}
          </div>
        </div>
      )}
    </main>
  )
}
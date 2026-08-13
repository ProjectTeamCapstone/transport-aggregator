import { useState } from 'react'
import OfferCard from './OfferCard'

/**
 * Cheapest and Fastest are two orderings of the SAME results, so switching
 * tabs never refetches - both lists arrive in one response.
 */
export default function ResultsTabs({ results, initialTab = 'cheapest' }) {
  const [tab, setTab] = useState(initialTab)

  if (!results) return null

  const offers = results[tab] || []
  const staleCount = results.stale_count || 0

  return (
    <section className="results">
      <div className="tabs" role="tablist">
        {['cheapest', 'fastest'].map((name) => (
          <button
            key={name}
            role="tab"
            aria-selected={tab === name}
            className={`tab ${tab === name ? 'tab--active' : ''}`}
            onClick={() => setTab(name)}
          >
            {name === 'cheapest' ? 'Cheapest' : 'Fastest'}
          </button>
        ))}
        <span className="results__count">
          {results.count} option{results.count === 1 ? '' : 's'}
        </span>
      </div>

      {staleCount > 0 && (
        <p className="notice notice--stale" data-testid="stale-notice">
          {staleCount} of these prices {staleCount === 1 ? 'has' : 'have'} not updated
          recently and may be out of date. They are shown rather than hidden.
        </p>
      )}

      {offers.length === 0 ? (
        <p className="notice" data-testid="empty">
          Nothing is running on that route for the date you chose. Try another date.
        </p>
      ) : (
        <div role="tabpanel" className="results__list">
          {offers.map((offer) => (
            <OfferCard key={offer.offer_id} offer={offer} />
          ))}
        </div>
      )}
    </section>
  )
}

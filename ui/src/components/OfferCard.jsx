import { useState } from 'react'
import { predict } from '../api'
import BookingPanel from './BookingPanel'
import PriceRiseBadge from './PriceRiseBadge'

export const formatNaira = (amount) =>
  `NGN ${Number(amount).toLocaleString('en-NG', { maximumFractionDigits: 0 })}`

/** "12h 00m" — travellers read hours, not 720 minutes. */
export const formatDuration = (minutes) => {
  const hours = Math.floor(minutes / 60)
  const rest = minutes % 60
  return hours ? `${hours}h ${String(rest).padStart(2, '0')}m` : `${rest}m`
}

export const formatDeparture = (iso) => iso.slice(11, 16)

export default function OfferCard({ offer }) {
  const [prediction, setPrediction] = useState(null)
  const [predicting, setPredicting] = useState(false)

  const askPrediction = async () => {
    setPredicting(true)
    try {
      setPrediction(await predict(offer.offer_id))
    } catch {
      // A missing prediction must not break the result card - the price and
      // the Book button are still useful without it.
      setPrediction({ unavailable: true })
    } finally {
      setPredicting(false)
    }
  }

  // Air journeys carry airport time at both ends, so the two numbers differ
  // and showing only the scheduled one would overstate how fast flying is.
  const hasGroundTime = offer.door_to_door_min !== offer.duration_min

  return (
    <article className="offer" data-testid="offer-card">
      <div className="offer__head">
        <span className={`mode mode--${offer.mode}`}>
          {offer.mode === 'air' ? 'Flight' : 'Coach'}
        </span>
        <h3>{offer.carrier}</h3>
        {offer.stale && (
          <span className="badge badge--stale" title={`Last updated ${Math.round(offer.age_seconds / 60)} minutes ago`}>
            Price may be out of date
          </span>
        )}
      </div>

      <dl className="offer__facts">
        <div><dt>Price</dt><dd className="offer__price">{formatNaira(offer.price_ngn)}</dd></div>
        <div><dt>Departs</dt><dd>{formatDeparture(offer.depart_time)}</dd></div>
        <div>
          <dt>Journey time</dt>
          <dd>
            {formatDuration(offer.door_to_door_min)}
            {hasGroundTime && (
              <small title="Includes getting to the airport, check-in, and the transfer at the other end">
                {' '}({formatDuration(offer.duration_min)} in the air)
              </small>
            )}
          </dd>
        </div>
        <div><dt>Seats left</dt><dd>{offer.seats_left}</dd></div>
      </dl>

      <div className="offer__actions">
        {prediction ? (
          <PriceRiseBadge prediction={prediction} />
        ) : (
          <button type="button" className="btn btn--ghost" onClick={askPrediction} disabled={predicting}>
            {predicting ? 'Checking…' : 'Will the price rise?'}
          </button>
        )}
        {/* Book goes through POST /book rather than straight to the carrier's
            page. That is what makes a double press safe and what records the
            attempt before anybody is called. */}
        <BookingPanel offer={offer} />
      </div>
    </article>
  )
}

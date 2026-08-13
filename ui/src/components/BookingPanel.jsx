import { useState } from 'react'
import { book } from '../api'
import { formatNaira } from './OfferCard'

const EMPTY_PASSENGER = { given_name: '', family_name: '', email: '' }

/**
 * What each booking state should say to a traveller, in their words rather than
 * ours. The states come straight from the ledger; only the wording is here.
 *
 * `pending` is the important one. For the carriers with no booking API it is the
 * final answer, not a stage on the way to `confirmed`, so it has to say clearly
 * that no seat is held yet.
 */
const HEADLINE = {
  confirmed: 'Seat held',
  pending: 'Almost there — finish on the carrier’s site',
  failed: 'Not booked',
  unknown: 'We are not sure yet',
}

export default function BookingPanel({ offer }) {
  const [open, setOpen] = useState(false)
  const [passenger, setPassenger] = useState(EMPTY_PASSENGER)
  const [submitting, setSubmitting] = useState(false)
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)

  const update = (field) => (event) =>
    setPassenger((current) => ({ ...current, [field]: event.target.value }))

  const submit = async (event) => {
    event.preventDefault()
    // Disabling the button is the polite guard. The real one is server-side:
    // the API derives an idempotency key from the request, so even a press that
    // slips through here cannot create a second booking.
    if (submitting) return

    setSubmitting(true)
    setError(null)
    try {
      setResult(
        await book({
          offerId: offer.offer_id,
          quotedPriceNgn: offer.price_ngn,
          passenger,
        })
      )
    } catch (err) {
      setError(err.message)
    } finally {
      setSubmitting(false)
    }
  }

  if (result) {
    return <BookingResult result={result} offer={offer} />
  }

  if (!open) {
    return (
      <button type="button" className="btn btn--primary" onClick={() => setOpen(true)}>
        Book
      </button>
    )
  }

  return (
    <form className="booking" onSubmit={submit} data-testid="booking-form">
      <p className="booking__note">
        Booking {offer.carrier} at {formatNaira(offer.price_ngn)}. We never ask for card
        details — payment happens with the carrier, not here.
      </p>

      <div className="booking__fields">
        <label>
          First name
          <input required value={passenger.given_name} onChange={update('given_name')} />
        </label>
        <label>
          Last name
          <input required value={passenger.family_name} onChange={update('family_name')} />
        </label>
        <label>
          Email
          <input required type="email" value={passenger.email} onChange={update('email')} />
        </label>
      </div>

      {error && (
        <p className="booking__error" data-testid="booking-error" role="alert">
          {error}
        </p>
      )}

      <div className="booking__actions">
        <button type="submit" className="btn btn--primary" disabled={submitting}>
          {submitting ? 'Booking…' : 'Confirm'}
        </button>
        <button
          type="button"
          className="btn btn--ghost"
          onClick={() => setOpen(false)}
          disabled={submitting}
        >
          Cancel
        </button>
      </div>
    </form>
  )
}

function BookingResult({ result, offer }) {
  const { state, detail, handoff_url: handoff, provider_reference: reference } = result

  return (
    <div className={`booking-result booking-result--${state}`} data-testid="booking-result" data-state={state}>
      <strong>{HEADLINE[state] || 'Booking'}</strong>

      {reference && (
        <p className="booking-result__reference" data-testid="booking-reference">
          Reference <code>{reference}</code>
        </p>
      )}

      <p>{detail}</p>

      {/* A 409 carries the fare that replaced the one they saw. Showing only
          "not booked" would leave them with nothing to act on. */}
      {result.live_price_ngn != null && state === 'failed' && (
        <p className="booking-result__prices" data-testid="booking-new-price">
          You saw {formatNaira(result.price_ngn_quoted)} — it is now{' '}
          {formatNaira(result.live_price_ngn)}. Search again to book at the new price.
        </p>
      )}

      {handoff && (
        <a className="btn btn--primary" href={handoff} target="_blank" rel="noreferrer">
          Continue on {offer.carrier}’s site
        </a>
      )}

      {result.replayed && (
        <p className="booking-result__replay" data-testid="booking-replayed">
          You had already submitted this booking, so this is the original one — not a
          second seat.
        </p>
      )}
    </div>
  )
}

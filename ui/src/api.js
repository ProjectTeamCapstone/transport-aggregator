// All calls go through /api, which Vite proxies to the FastAPI service in dev.
// One origin in the browser means no CORS configuration to get wrong.

const BASE = '/api'

async function get(path, params = {}) {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== '')
  )
  const response = await fetch(`${BASE}${path}?${query}`)
  const body = await response.json().catch(() => ({}))

  if (!response.ok) {
    // The API returns a structured `detail` for a query it could not
    // interpret. Surfacing that verbatim is far more useful than "400".
    const detail = body.detail
    const message =
      typeof detail === 'string'
        ? detail
        : detail?.message || `Request failed (${response.status})`
    const error = new Error(message)
    error.detail = detail
    error.status = response.status
    throw error
  }
  return body
}

async function post(path, payload) {
  const response = await fetch(`${BASE}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  const body = await response.json().catch(() => ({}))

  // 409 means the fare moved between searching and booking. That is an outcome
  // the traveller has to see — it carries both the old and the new price — not
  // an error to swallow, so it comes back as data like any other result.
  if (response.ok || response.status === 409) return body

  const detail = body.detail
  const message =
    typeof detail === 'string' ? detail : detail?.message || `Request failed (${response.status})`
  const error = new Error(message)
  error.status = response.status
  error.body = body
  throw error
}

export const searchStructured = ({ origin, dest, date, limit = 10 }) =>
  get('/search', { origin, dest, date, limit })

export const searchNatural = (q, limit = 10) => get('/search/nl', { q, limit })

export const predict = (offerId) => get('/predict', { offer_id: offerId })

export const health = () => get('/health')

/**
 * Book an offer.
 *
 * `quoted_price_ngn` is the price the traveller actually saw. The API checks it
 * against the live fare before booking anything, so sending the displayed value
 * rather than re-reading it is the point, not a shortcut.
 *
 * No idempotency key is sent. The API derives one from the request contents, so
 * a double-clicked button cannot produce two bookings even though this call
 * carries nothing to tie the two presses together.
 */
export const book = ({ offerId, quotedPriceNgn, passenger }) =>
  post('/book', {
    offer_id: offerId,
    quoted_price_ngn: quotedPriceNgn,
    passenger,
  })

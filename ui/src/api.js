// All calls go through /api, which Vite proxies to the FastAPI service on Azure.
const BASE = '/api'

/**
 * Maps raw backend/network errors to friendly user-facing messages
 */
function formatFriendlyError(errStatus, detail) {
  if (errStatus === 400) {
    if (typeof detail === 'string' && detail.includes('out of scope')) {
      return {
        message: "That travel route is currently outside our service coverage.",
        hint: "NaijaFare currently covers travel between Lagos (LOS), Abuja (ABV), Port Harcourt (PHC), Onitsha (ONI), and London Heathrow (LHR)."
      }
    }
    return {
      message: typeof detail === 'string' ? detail : detail?.message || "Invalid search request. Please check your selected route.",
      hint: detail?.hint || "Try selecting different origin and destination cities."
    }
  }

  if (errStatus === 404) {
    return {
      message: "No price history or active fares found for this selection.",
      hint: "Try picking an upcoming date or checking an alternative travel mode."
    }
  }

  if (errStatus >= 500) {
    return {
      message: "Our fare search service is currently undergoing quick maintenance.",
      hint: "Please try again in a few moments."
    }
  }

  return {
    message: "Unable to retrieve real-time fares right now.",
    hint: "Please check your network connection or try searching again."
  }
}

async function get(path, params = {}) {
  const query = new URLSearchParams(
    Object.entries(params).filter(([, v]) => v !== null && v !== undefined && v !== '')
  ).toString()

  const url = query ? `${BASE}${path}?${query}` : `${BASE}${path}`
  
  let response
  try {
    response = await fetch(url)
  } catch (netErr) {
    const friendly = formatFriendlyError(0, null)
    const err = new Error(friendly.message)
    err.detail = { hint: friendly.hint }
    throw err
  }

  const body = await response.json().catch(() => ({}))

  if (!response.ok) {
    const detail = body.detail
    const friendly = formatFriendlyError(response.status, detail)
    const error = new Error(friendly.message)
    error.detail = { hint: friendly.hint || (typeof detail === 'object' ? detail?.hint : null) }
    error.status = response.status
    throw error
  }

  return body
}

async function post(path, payload) {
  let response
  try {
    response = await fetch(`${BASE}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
  } catch (netErr) {
    const friendly = formatFriendlyError(0, null)
    const err = new Error(friendly.message)
    err.detail = { hint: friendly.hint }
    throw err
  }

  const body = await response.json().catch(() => ({}))

  if (response.ok || response.status === 409) return body

  const detail = body.detail
  const friendly = formatFriendlyError(response.status, detail)
  const error = new Error(friendly.message)
  error.status = response.status
  error.detail = { hint: friendly.hint }
  throw error
}

export const searchStructured = async ({ origin, dest, destination, date, limit = 10 }) => {
  const data = await get('/search', { 
    origin: origin?.toUpperCase(), 
    dest: (dest || destination)?.toUpperCase(), 
    date, 
    limit 
  })

  if (!data || typeof data !== 'object') {
    throw new Error("We couldn't retrieve valid fares for this selection. Please try again.")
  }

  return data
}

export const searchNatural = (q, limit = 10) => get('/search/nl', { q, limit })
export const predict = (offerId) => get('/predict', { offer_id: offerId })
export const fetchPricePrediction = predict
export const bookOptions = (offerId) => get('/book/options', { offer_id: offerId })
export const fetchBookOptions = bookOptions
export const health = () => get('/health')

export const book = (args = {}) => {
  const offer_id = args.offerId || args.offer_id
  const quoted_price_ngn = args.quotedPriceNgn || args.quoted_price_ngn
  const passenger = args.passenger

  return post('/book', {
    offer_id,
    quoted_price_ngn,
    passenger,
  })
}
export const createBooking = book
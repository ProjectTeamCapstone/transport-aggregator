import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import OfferCard, { formatNaira, formatDuration, formatDeparture } from './OfferCard'
import { predict } from '../api'

vi.mock('../api', () => ({ predict: vi.fn(), book: vi.fn() }))

const coach = (over = {}) => ({
  offer_id: 'gigm-road-LOS-ABV-20260914T0600Z',
  carrier: 'GIGM', mode: 'road', origin: 'LOS', destination: 'ABV',
  depart_time: '2026-09-14T07:00:00+01:00',
  duration_min: 720, door_to_door_min: 720,
  price_ngn: 42500, seats_left: 12,
  booking_url: 'https://gigm.example/book',
  stale: false, age_seconds: 30, ...over,
})

const flight = (over = {}) => coach({
  offer_id: 'airpeace-air-LOS-ABV-20260914T0600Z',
  carrier: 'Air Peace', mode: 'air',
  duration_min: 75, door_to_door_min: 315, price_ngn: 145000,
  booking_url: 'https://flyairpeace.example/book', ...over,
})

beforeEach(() => vi.clearAllMocks())

describe('formatting', () => {
  it('writes prices as whole naira with separators', () => {
    expect(formatNaira(42500)).toBe('NGN 42,500')
  })
  it('writes durations in hours and minutes, not raw minutes', () => {
    expect(formatDuration(720)).toBe('12h 00m')
    expect(formatDuration(75)).toBe('1h 15m')
    expect(formatDuration(45)).toBe('45m')
  })
  it('shows the local departure time, not the UTC instant', () => {
    // 07:00+01:00 is 06:00Z. A traveller must see 07:00 — the time they have
    // to be at the terminal.
    expect(formatDeparture('2026-09-14T07:00:00+01:00')).toBe('07:00')
  })
})

describe('OfferCard', () => {
  it('shows the carrier, price and departure', () => {
    render(<OfferCard offer={coach()} />)
    expect(screen.getByRole('heading')).toHaveTextContent('GIGM')
    expect(screen.getByText('NGN 42,500')).toBeInTheDocument()
    expect(screen.getByText('07:00')).toBeInTheDocument()
  })

  it('labels a coach and a flight differently', () => {
    const { unmount } = render(<OfferCard offer={coach()} />)
    expect(screen.getByText('Coach')).toBeInTheDocument()
    unmount()
    render(<OfferCard offer={flight()} />)
    expect(screen.getByText('Flight')).toBeInTheDocument()
  })

  it('shows door-to-door time for a flight, with the in-air time beside it', () => {
    // The honesty requirement: a 75-minute flight is a 315-minute journey.
    render(<OfferCard offer={flight()} />)
    expect(screen.getByText(/5h 15m/)).toBeInTheDocument()
    expect(screen.getByText(/1h 15m in the air/)).toBeInTheDocument()
  })

  it('shows a single duration for a coach, which is already door to door', () => {
    render(<OfferCard offer={coach()} />)
    expect(screen.getByText('12h 00m')).toBeInTheDocument()
    expect(screen.queryByText(/in the air/)).not.toBeInTheDocument()
  })

  it('flags a stale price on the card itself', () => {
    render(<OfferCard offer={coach({ stale: true, age_seconds: 1800 })} />)
    expect(screen.getByText(/may be out of date/i)).toBeInTheDocument()
  })

  it('offers Book as an action, not a link straight to the carrier', async () => {
    // Book now goes through POST /book, which records the attempt before anyone
    // is called and makes a second press harmless. Jumping straight to the
    // carrier's page would skip both.
    render(<OfferCard offer={coach()} />)
    expect(screen.queryByRole('link', { name: 'Book' })).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: 'Book' }))
    expect(screen.getByTestId('booking-form')).toBeInTheDocument()
  })

  it('asks for a prediction only when the user requests one', async () => {
    predict.mockResolvedValue({ will_rise: true, probability: 0.78, model_version: 'lgbm-1' })
    render(<OfferCard offer={coach()} />)
    expect(predict).not.toHaveBeenCalled()

    await userEvent.click(screen.getByRole('button', { name: /will the price rise/i }))
    expect(predict).toHaveBeenCalledWith(coach().offer_id)
    expect(await screen.findByTestId('prediction')).toHaveTextContent('Likely to rise (78%)')
  })

  it('keeps the card usable when the prediction fails', async () => {
    predict.mockRejectedValue(new Error('model down'))
    render(<OfferCard offer={coach()} />)
    await userEvent.click(screen.getByRole('button', { name: /will the price rise/i }))

    expect(await screen.findByTestId('prediction')).toHaveTextContent(/unavailable/i)
    // The price and the Book button still work — a missing prediction is not
    // a reason to break the result.
    expect(screen.getByText('NGN 42,500')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Book' })).toBeInTheDocument()
  })
})

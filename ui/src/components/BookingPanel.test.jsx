import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import BookingPanel from './BookingPanel'
import { book } from '../api'

vi.mock('../api', () => ({ book: vi.fn() }))

const offer = (over = {}) => ({
  offer_id: 'gigm-road-LOS-ABV-20260914T0600Z',
  carrier: 'GIGM', mode: 'road', origin: 'LOS', destination: 'ABV',
  depart_time: '2026-09-14T07:00:00+01:00',
  duration_min: 720, door_to_door_min: 720,
  price_ngn: 42500, seats_left: 12,
  booking_url: 'https://gigm.example/book',
  stale: false, age_seconds: 30, ...over,
})

const response = (over = {}) => ({
  idempotency_key: 'auto-v1-abc123', offer_id: offer().offer_id, carrier: 'GIGM',
  state: 'pending', provider: 'deeplink', provider_reference: null,
  price_ngn_quoted: 42500, price_ngn_at_book: 42500, attempts: 1,
  last_error: null, replayed: false, live_price_ngn: 42500,
  handoff_url: 'https://gigm.example/book?from=LOS&to=ABV',
  detail: 'GIGM has no booking API we can use, so this is a pre-filled link to '
    + 'their own checkout. Your seat is not reserved until you complete it there.',
  ...over,
})

const fillIn = async () => {
  await userEvent.type(screen.getByLabelText('First name'), 'Ada')
  await userEvent.type(screen.getByLabelText('Last name'), 'Okafor')
  await userEvent.type(screen.getByLabelText('Email'), 'ada@example.test')
}

beforeEach(() => vi.clearAllMocks())

describe('BookingPanel', () => {
  it('asks for nothing until Book is pressed', () => {
    render(<BookingPanel offer={offer()} />)
    expect(screen.getByRole('button', { name: 'Book' })).toBeInTheDocument()
    expect(screen.queryByTestId('booking-form')).not.toBeInTheDocument()
  })

  it('never asks for card details', async () => {
    // Payment processing is out of scope, and the form is where that promise is
    // either kept or broken.
    render(<BookingPanel offer={offer()} />)
    await userEvent.click(screen.getByRole('button', { name: 'Book' }))

    expect(screen.queryByLabelText(/card/i)).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/cvv/i)).not.toBeInTheDocument()
    expect(screen.getByTestId('booking-form')).toHaveTextContent(/never ask for card details/i)
  })

  it('sends the price the traveller actually saw', async () => {
    // Not a re-read of the live fare: this value is the record of what they
    // agreed to, which is exactly what the API checks against.
    book.mockResolvedValue(response())
    render(<BookingPanel offer={offer()} />)
    await userEvent.click(screen.getByRole('button', { name: 'Book' }))
    await fillIn()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    expect(book).toHaveBeenCalledWith({
      offerId: offer().offer_id,
      quotedPriceNgn: 42500,
      passenger: { given_name: 'Ada', family_name: 'Okafor', email: 'ada@example.test' },
    })
  })

  it('says a coach seat is not reserved yet, and links onward', async () => {
    book.mockResolvedValue(response())
    render(<BookingPanel offer={offer()} />)
    await userEvent.click(screen.getByRole('button', { name: 'Book' }))
    await fillIn()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    const result = await screen.findByTestId('booking-result')
    expect(result).toHaveAttribute('data-state', 'pending')
    expect(result).toHaveTextContent(/finish on the carrier/i)
    expect(result).toHaveTextContent(/not reserved/i)

    const onward = screen.getByRole('link', { name: /Continue on GIGM/ })
    expect(onward).toHaveAttribute('href', 'https://gigm.example/book?from=LOS&to=ABV')
    expect(onward).toHaveAttribute('rel', 'noreferrer')
  })

  it('shows the reference when a seat really was held', async () => {
    book.mockResolvedValue(response({
      state: 'confirmed', provider: 'duffel_sandbox', provider_reference: 'NF1001',
      handoff_url: null, detail: 'Held with Air Peace through the Duffel sandbox.',
    }))
    render(<BookingPanel offer={offer({ carrier: 'Air Peace', mode: 'air' })} />)
    await userEvent.click(screen.getByRole('button', { name: 'Book' }))
    await fillIn()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    expect(await screen.findByTestId('booking-result')).toHaveAttribute('data-state', 'confirmed')
    expect(screen.getByTestId('booking-reference')).toHaveTextContent('NF1001')
  })

  it('shows both prices when the fare moved', async () => {
    // "Not booked" on its own leaves the traveller with nothing to act on.
    book.mockResolvedValue(response({
      state: 'failed', price_ngn_quoted: 37500, live_price_ngn: 42500,
      handoff_url: null,
      detail: 'The fare has risen 13.3% since you saw it. Nothing was booked.',
    }))
    render(<BookingPanel offer={offer()} />)
    await userEvent.click(screen.getByRole('button', { name: 'Book' }))
    await fillIn()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    const moved = await screen.findByTestId('booking-new-price')
    expect(moved).toHaveTextContent('NGN 37,500')
    expect(moved).toHaveTextContent('NGN 42,500')
    expect(moved).toHaveTextContent(/search again/i)
  })

  it('admits it does not know when the carrier never answered', async () => {
    // The state that must not be dressed up as either success or failure.
    book.mockResolvedValue(response({
      state: 'unknown', handoff_url: null,
      detail: 'We asked the carrier to book this and never got an answer, so we '
        + 'do not know whether a seat was taken.',
    }))
    render(<BookingPanel offer={offer()} />)
    await userEvent.click(screen.getByRole('button', { name: 'Book' }))
    await fillIn()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    const result = await screen.findByTestId('booking-result')
    expect(result).toHaveAttribute('data-state', 'unknown')
    expect(result).toHaveTextContent(/not sure yet/i)
    expect(screen.queryByTestId('booking-reference')).not.toBeInTheDocument()
  })

  it('explains a replay rather than showing it as a fresh booking', async () => {
    book.mockResolvedValue(response({ replayed: true }))
    render(<BookingPanel offer={offer()} />)
    await userEvent.click(screen.getByRole('button', { name: 'Book' }))
    await fillIn()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    expect(await screen.findByTestId('booking-replayed')).toHaveTextContent(/not a second seat/i)
  })

  it('only submits once however fast Confirm is pressed', async () => {
    // The UI guard. The binding promise is server-side, but sending one request
    // instead of three is still the right behaviour here.
    let release
    book.mockReturnValue(new Promise((resolve) => { release = resolve }))
    render(<BookingPanel offer={offer()} />)
    await userEvent.click(screen.getByRole('button', { name: 'Book' }))
    await fillIn()

    const confirm = screen.getByRole('button', { name: 'Confirm' })
    await userEvent.click(confirm)
    expect(confirm).toBeDisabled()
    await userEvent.click(confirm)
    await userEvent.click(confirm)

    expect(book).toHaveBeenCalledTimes(1)

    // Let the in-flight request settle before the test ends, so React applies
    // the resulting state update inside the test rather than after it.
    release(response())
    expect(await screen.findByTestId('booking-result')).toBeInTheDocument()
  })

  it('keeps the form usable when the request itself fails', async () => {
    book.mockRejectedValue(new Error('The booking ledger is unreachable'))
    render(<BookingPanel offer={offer()} />)
    await userEvent.click(screen.getByRole('button', { name: 'Book' }))
    await fillIn()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm' }))

    expect(await screen.findByTestId('booking-error')).toHaveTextContent(/ledger is unreachable/)
    // Still on the form, with their details intact, so they can simply retry.
    expect(screen.getByLabelText('First name')).toHaveValue('Ada')
    expect(screen.getByRole('button', { name: 'Confirm' })).toBeEnabled()
  })

  it('can be backed out of without booking anything', async () => {
    render(<BookingPanel offer={offer()} />)
    await userEvent.click(screen.getByRole('button', { name: 'Book' }))
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(screen.getByRole('button', { name: 'Book' })).toBeInTheDocument()
    expect(book).not.toHaveBeenCalled()
  })
})

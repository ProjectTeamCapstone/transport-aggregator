import { describe, it, expect, vi, beforeEach } from 'vitest'
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import ResultsTabs from './ResultsTabs'

vi.mock('../api', () => ({ predict: vi.fn() }))

const coach = (over = {}) => ({
  offer_id: 'gigm-road-LOS-ABV-20260914T0600Z',
  carrier: 'GIGM', mode: 'road', origin: 'LOS', destination: 'ABV',
  depart_time: '2026-09-14T07:00:00+01:00',
  duration_min: 720, door_to_door_min: 720,
  price_ngn: 42500, seats_left: 12,
  booking_url: 'https://gigm.example/book',
  stale: false, age_seconds: 30, ...over,
})

const flight = (over = {}) => ({
  offer_id: 'airpeace-air-LOS-ABV-20260914T0600Z',
  carrier: 'Air Peace', mode: 'air', origin: 'LOS', destination: 'ABV',
  depart_time: '2026-09-14T07:00:00+01:00',
  duration_min: 75, door_to_door_min: 315,
  price_ngn: 145000, seats_left: 40,
  booking_url: 'https://flyairpeace.example/book',
  stale: false, age_seconds: 30, ...over,
})

// Cheapest and Fastest are two orderings of the same three offers.
const results = (over = {}) => ({
  count: 2, stale: false, stale_count: 0,
  cheapest: [coach(), flight()],
  fastest: [flight(), coach()],
  ...over,
})

beforeEach(() => vi.clearAllMocks())

describe('ResultsTabs', () => {
  it('shows the cheapest list first by default', () => {
    render(<ResultsTabs results={results()} />)
    const cards = screen.getAllByTestId('offer-card')
    expect(within(cards[0]).getByRole('heading')).toHaveTextContent('GIGM')
  })

  it('honours the tab the search asked for', () => {
    render(<ResultsTabs results={results()} initialTab="fastest" />)
    const cards = screen.getAllByTestId('offer-card')
    expect(within(cards[0]).getByRole('heading')).toHaveTextContent('Air Peace')
  })

  it('reorders when the other tab is chosen', async () => {
    render(<ResultsTabs results={results()} />)
    await userEvent.click(screen.getByRole('tab', { name: 'Fastest' }))
    const cards = screen.getAllByTestId('offer-card')
    expect(within(cards[0]).getByRole('heading')).toHaveTextContent('Air Peace')
  })

  it('marks the active tab for assistive technology', async () => {
    render(<ResultsTabs results={results()} />)
    expect(screen.getByRole('tab', { name: 'Cheapest' })).toHaveAttribute('aria-selected', 'true')
    await userEvent.click(screen.getByRole('tab', { name: 'Fastest' }))
    expect(screen.getByRole('tab', { name: 'Fastest' })).toHaveAttribute('aria-selected', 'true')
  })

  it('switching tabs does not refetch — both lists are already loaded', async () => {
    const { predict } = await import('../api')
    render(<ResultsTabs results={results()} />)
    await userEvent.click(screen.getByRole('tab', { name: 'Fastest' }))
    await userEvent.click(screen.getByRole('tab', { name: 'Cheapest' }))
    expect(predict).not.toHaveBeenCalled()
  })

  it('explains an empty route instead of showing a blank page', () => {
    render(<ResultsTabs results={results({ count: 0, cheapest: [], fastest: [] })} />)
    expect(screen.getByTestId('empty')).toHaveTextContent(/nothing is running/i)
  })

  it('renders nothing before the first search', () => {
    const { container } = render(<ResultsTabs results={null} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('warns when some prices are stale, and still shows them', () => {
    render(<ResultsTabs results={results({ stale_count: 1, cheapest: [coach({ stale: true }), flight()] })} />)
    expect(screen.getByTestId('stale-notice')).toBeInTheDocument()
    // The stale price must still be listed — hiding it would leave a blank
    // page and conceal the fault that caused it.
    expect(screen.getAllByTestId('offer-card')).toHaveLength(2)
  })

  it('pluralises the result count', () => {
    render(<ResultsTabs results={results({ count: 1, cheapest: [coach()] })} />)
    expect(screen.getByText('1 option')).toBeInTheDocument()
  })
})

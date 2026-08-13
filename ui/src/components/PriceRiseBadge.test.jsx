import { describe, it, expect } from 'vitest'
import { render, screen } from '@testing-library/react'
import PriceRiseBadge from './PriceRiseBadge'

const p = (over = {}) => ({
  will_rise: true, probability: 0.78, model_version: 'lgbm-202608121138', ...over,
})

describe('PriceRiseBadge', () => {
  it('shows a confident rise with its probability', () => {
    render(<PriceRiseBadge prediction={p()} />)
    expect(screen.getByTestId('prediction')).toHaveTextContent('Likely to rise (78%)')
  })

  it('shows a confident hold', () => {
    render(<PriceRiseBadge prediction={p({ will_rise: false, probability: 0.12 })} />)
    expect(screen.getByTestId('prediction')).toHaveTextContent('Unlikely to rise (12%)')
  })

  it('admits uncertainty near the middle rather than picking a side', () => {
    // A 52% prediction is nearly a coin toss. Rendering it as a confident
    // "likely to rise" would present a guess as advice.
    render(<PriceRiseBadge prediction={p({ probability: 0.52 })} />)
    expect(screen.getByTestId('prediction')).toHaveTextContent('Could go either way (52%)')
  })

  it('treats the boundaries of the unsure band as unsure', () => {
    for (const probability of [0.4, 0.6]) {
      const { unmount } = render(<PriceRiseBadge prediction={p({ probability })} />)
      expect(screen.getByTestId('prediction')).toHaveTextContent(/either way/)
      unmount()
    }
  })

  it('discloses that the model was trained on simulated data', () => {
    render(<PriceRiseBadge prediction={p()} />)
    expect(screen.getByTestId('prediction')).toHaveAttribute(
      'title', expect.stringContaining('simulated')
    )
  })

  it('says so plainly when no prediction is available', () => {
    render(<PriceRiseBadge prediction={{ unavailable: true }} />)
    expect(screen.getByTestId('prediction')).toHaveTextContent('Prediction unavailable')
  })
})

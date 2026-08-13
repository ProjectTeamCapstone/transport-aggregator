/**
 * Shows the model's answer as a confidence, not a verdict.
 *
 * "Likely to rise (78%)" tells a traveller far more than a bare yes - near the
 * middle the model is least sure, and a yes/no badge hides exactly that.
 */
export default function PriceRiseBadge({ prediction }) {
  if (prediction.unavailable) {
    return (
      <span className="badge badge--unknown" data-testid="prediction">
        Prediction unavailable
      </span>
    )
  }

  const percent = Math.round(prediction.probability * 100)
  const rising = prediction.will_rise
  // Between 40% and 60% the model is close to a coin toss; saying so is more
  // honest than dressing it up as a direction.
  const uncertain = percent >= 40 && percent <= 60

  const label = uncertain
    ? `Could go either way (${percent}%)`
    : rising
      ? `Likely to rise (${percent}%)`
      : `Unlikely to rise (${percent}%)`

  return (
    <span
      className={`badge ${uncertain ? 'badge--unsure' : rising ? 'badge--rise' : 'badge--steady'}`}
      data-testid="prediction"
      title={
        `Chance this price is higher in 24 hours: ${percent}%. ` +
        `Model ${prediction.model_version}, trained on simulated data.`
      }
    >
      {label}
    </span>
  )
}

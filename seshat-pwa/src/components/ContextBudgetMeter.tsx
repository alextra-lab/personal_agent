'use client';

interface ContextBudgetMeterProps {
  /** Utilisation ratio in [0, 1]. Null hides the meter. */
  budget: number | null;
}

/**
 * Horizontal meter showing context window utilisation.
 *
 * Driven by AG-UI STATE_DELTA events with key ``context_window``.
 * Colour transitions from green → amber → red as budget is consumed.
 * Hidden when no STATE_DELTA has been received yet (budget is null).
 */
export function ContextBudgetMeter({ budget }: ContextBudgetMeterProps) {
  if (budget === null) return null;

  const pct = Math.round(Math.min(Math.max(budget, 0), 1) * 100);

  const barColor =
    pct < 60
      ? 'bg-emerald-500'
      : pct < 80
      ? 'bg-amber-500'
      : 'bg-red-500';

  const labelColor =
    pct < 60
      ? 'text-emerald-400'
      : pct < 80
      ? 'text-amber-400'
      : 'text-red-400';

  return (
    <div
      className="px-4 py-1.5 flex items-center gap-2"
      title={`Context window: ${pct}% used`}
    >
      <span className="text-xs text-slate-500 flex-shrink-0">ctx</span>
      {/* Track */}
      <div className="flex-1 h-1 bg-slate-700 rounded-full overflow-hidden">
        {/* Fill */}
        <div
          className={`h-full rounded-full transition-all duration-300 ${barColor}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className={`text-xs font-mono flex-shrink-0 ${labelColor}`}>
        {pct}%
      </span>
    </div>
  );
}

'use client';

/**
 * Per-turn 0–3 value rating control (FRE-407).
 *
 * Renders a compact 4-segment meter in the assistant message footer,
 * beside the copy button, using the same hover-reveal idiom.
 *
 * Rating scale and fill colors (accent vocabulary from the existing PWA):
 *   0 — No value        → slate-600  (neutral/empty)
 *   1 — Low value       → amber-400  (caution)
 *   2 — Meets expectation → sky-400  (informational)
 *   3 — Wow             → emerald-400 (confirmed/good)
 *
 * Behaviour:
 *   - Optimistic select on click; reverts to previous value on network failure.
 *   - On confirmation a single animate-pulse fires then the state settles.
 *   - Re-rating is allowed: clicking a new segment re-POSTs and overwrites.
 *   - Never renders mid-stream; parent gates on `message.complete === true`.
 */

import { useState, useCallback } from 'react';
import { submitTurnRating } from '@/lib/submitTurnRating';

// ─────────────────────────────────────────────────────────────────────────────
// Rating metadata
// ─────────────────────────────────────────────────────────────────────────────

interface RatingMeta {
  label: string;
  /** Tailwind fill class applied to segments at-or-below the selected index. */
  fillClass: string;
}

const RATING_META: [RatingMeta, RatingMeta, RatingMeta, RatingMeta] = [
  { label: 'No value',            fillClass: 'bg-slate-600' },
  { label: 'Low value',           fillClass: 'bg-amber-400' },
  { label: 'Meets expectation',   fillClass: 'bg-sky-400'   },
  { label: 'Wow',                 fillClass: 'bg-emerald-400' },
];

// ─────────────────────────────────────────────────────────────────────────────
// Component
// ─────────────────────────────────────────────────────────────────────────────

interface TurnRatingProps {
  /** Trace ID of the rated assistant turn — the ES join key (FRE-407). */
  traceId: string;
  /** Session that owns the turn — enforced server-side for ownership. */
  sessionId: string;
}

/**
 * Compact 4-segment value-rating meter.
 *
 * Follows the CopyButton hover-reveal pattern: `opacity-0 group-hover:opacity-100
 * focus-within:opacity-100 transition-opacity`. Placed beside the copy button
 * in the assistant message footer.
 *
 * Args:
 *   traceId:   Trace ID of the assistant turn to rate.
 *   sessionId: Owning session ID, forwarded to the rating endpoint.
 */
export function TurnRating({ traceId, sessionId }: TurnRatingProps) {
  /** null = no rating submitted yet; number = persisted score. */
  const [persisted, setPersisted] = useState<number | null>(null);
  /** Optimistic selection while the request is in-flight. */
  const [optimistic, setOptimistic] = useState<number | null>(null);
  /** Drives the pulse animation on successful confirmation. */
  const [pulsing, setPulsing] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const currentRating = optimistic ?? persisted;

  const handleRate = useCallback(
    async (rating: number) => {
      if (submitting) return;

      const previous = persisted;

      // Optimistic update — paint immediately.
      setOptimistic(rating);
      setSubmitting(true);

      const ok = await submitTurnRating(traceId, sessionId, rating);

      setSubmitting(false);
      setOptimistic(null);

      if (ok) {
        setPersisted(rating);
        // Pulse the confirmed segment, then settle.
        setPulsing(rating);
        setTimeout(() => setPulsing(null), 800);
      } else {
        // Revert to previous persisted value on failure.
        setPersisted(previous);
      }
    },
    [traceId, sessionId, persisted, submitting],
  );

  return (
    <div
      className="opacity-0 group-hover:opacity-100 focus-within:opacity-100 transition-opacity flex items-center gap-0.5"
      aria-label="Rate this response"
    >
      {RATING_META.map((meta, index) => {
        const isFilled = currentRating !== null && index <= currentRating;
        const fillClass = isFilled ? RATING_META[currentRating].fillClass : 'bg-slate-700';
        const isPulsing = pulsing !== null && index === pulsing;

        return (
          <button
            key={index}
            onClick={() => void handleRate(index)}
            title={meta.label}
            aria-label={meta.label}
            disabled={submitting}
            className={[
              'w-3 h-1.5 rounded-sm transition-colors',
              'hover:opacity-80 disabled:cursor-not-allowed',
              fillClass,
              isPulsing ? 'animate-pulse' : '',
            ]
              .filter(Boolean)
              .join(' ')}
          />
        );
      })}
    </div>
  );
}

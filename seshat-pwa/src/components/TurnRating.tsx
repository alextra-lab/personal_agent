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
 *   - Default displayed value is 2 ("Meets expectation", sky-400) — purely
 *     visual; no network POST is issued on mount or render.
 *   - A rating is sent ONLY on an explicit user click (including re-clicking
 *     segment 2 to deliberately confirm a 2).
 *   - Each turn's control independently defaults to 2 until clicked.
 *   - Optimistic select on click; reverts to previous visual state on failure.
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
  /**
   * Previously-submitted 0–3 score, hydrated from history (FRE-426). When
   * present the control renders solid (rated); when undefined it renders the
   * faint unrated default.
   */
  initialRating?: number;
}

/**
 * Compact 4-segment value-rating meter.
 *
 * Persistently visible in the assistant message footer (NOT hover-gated like
 * the copy button) — the rating is a primary affordance the user engages every
 * turn, and a hover-reveal made it invisible on resize and on touch devices.
 *
 * Default display: segment 2 ("Meets expectation", sky-400) renders filled
 * before any interaction. This is purely visual — no POST is issued until
 * the user explicitly clicks a segment. Un-clicked controls are treated as
 * value 2 by the backend imputation metric (FRE-407).
 *
 * Args:
 *   traceId:   Trace ID of the assistant turn to rate.
 *   sessionId: Owning session ID, forwarded to the rating endpoint.
 */
export function TurnRating({ traceId, sessionId, initialRating }: TurnRatingProps) {
  /**
   * null = no rating submitted yet (visually defaults to 2, not persisted).
   * number = persisted score (hydrated from history or set by the user).
   */
  const [persisted, setPersisted] = useState<number | null>(initialRating ?? null);
  /** Optimistic selection while the request is in-flight. */
  const [optimistic, setOptimistic] = useState<number | null>(null);
  /** Drives the pulse animation on successful confirmation. */
  const [pulsing, setPulsing] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);

  /**
   * Visual rating: defaults to 2 before any click so the meter shows
   * "Meets expectation" at rest. Actual persisted value is null until the
   * user explicitly clicks.
   */
  const VISUAL_DEFAULT = 2;
  const currentRating = optimistic ?? persisted ?? VISUAL_DEFAULT;
  // Distinguish "showing the default" from "the user actually rated this".
  // Unrated → faint (light blue); explicitly rated → solid (darker). Gives the
  // user a clear at-a-glance indicator of which turns they've scored.
  const isExplicit = (optimistic ?? persisted) !== null;

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
      className={`flex items-center gap-0.5 transition-opacity ${
        isExplicit ? 'opacity-100' : 'opacity-40 hover:opacity-70'
      }`}
      title={isExplicit ? 'You rated this turn — click to change' : 'Not yet rated — click to rate'}
      aria-label={isExplicit ? 'Rated — change your rating' : 'Rate this response'}
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

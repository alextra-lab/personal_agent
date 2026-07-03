'use client';

/**
 * Per-turn quality rating control (FRE-407 → redesigned in FRE-757).
 *
 * Renders three tap-sized icon chips in the assistant message footer:
 *
 *   error ✕  → store value 0  (dark red)
 *   ok    ✓  → store value 2  (green — the resting default)
 *   exceptional ★ → store value 3  (gold)
 *
 * Design (owner-confirmed 2026-07-03):
 *   - The store keeps the FRE-407 0–3 integer scale; the control exposes three
 *     of the four values. ok=2 deliberately equals the backend imputation
 *     default, so persisting/backfilling "ok" is metric-invariant.
 *   - Legacy stored value 1 ("Low", no longer offered) hydrates as a distinct
 *     "legacy low" state on the error chip; the stored value is left untouched
 *     until the user re-rates.
 *   - Resting state reads as ok (solid green) — a persisted value, not a faint
 *     "unset" default. The persisted-on-send write is issued by the streaming
 *     hook on turn completion (useSSEStream DONE), NOT by this component, so
 *     no POST is ever fired on mount / hydration / replay.
 *   - Touch targets are 44×44px (Apple HIG / WCAG 2.5.5) around a ~24px icon.
 *
 * Behaviour (unchanged from FRE-407/426):
 *   - Persistently visible (never hover-gated).
 *   - Optimistic paint + a single pulse on confirmation.
 *   - Re-rating overwrites (a manual click always POSTs, incl. re-confirming ok).
 *   - Prior ratings hydrate from history via `initialRating`.
 *   - Never renders mid-stream — the parent gates on `message.complete`.
 */

import { useState, useCallback } from 'react';
import { submitTurnRating } from '@/lib/submitTurnRating';

// ─────────────────────────────────────────────────────────────────────────────
// Chip vocabulary
// ─────────────────────────────────────────────────────────────────────────────

type ChipKey = 'error' | 'ok' | 'exceptional';

interface Chip {
  key: ChipKey;
  /** Store value this chip writes (FRE-407 0–3 scale; 1 "Low" is not offered). */
  value: number;
  glyph: string;
  label: string;
  /** Tailwind classes applied when this chip is the selected rating. */
  selectedClass: string;
}

/** The resting default: "ok" (store value 2 == the backend imputation default). */
const DEFAULT_OK = 2;
/** Legacy "Low" store value, hydrated-only — no chip writes it. */
const LEGACY_LOW = 1;

const CHIPS: readonly Chip[] = [
  {
    key: 'error',
    value: 0,
    glyph: '✕',
    label: 'error',
    // #991b1b / #b91c1c / #fecaca
    selectedClass: 'bg-red-800 border-red-700 text-red-200',
  },
  {
    key: 'ok',
    value: DEFAULT_OK,
    glyph: '✓',
    label: 'ok',
    // #059669 / #10b981 / #d1fae5 (emerald-600/500/100)
    selectedClass: 'bg-emerald-600 border-emerald-500 text-emerald-100',
  },
  {
    key: 'exceptional',
    value: 3,
    glyph: '★',
    label: 'exceptional',
    // #d4af37 / #e6c34e / #1a1205 + subtle glow
    selectedClass:
      'bg-[#d4af37] border-[#e6c34e] text-[#1a1205] shadow-[0_0_8px_rgba(212,175,55,0.55)]',
  },
];

/** Muted resting style for a chip that is not the selected rating. */
const UNSELECTED_CLASS =
  'bg-transparent border-transparent text-slate-500 hover:text-slate-300 hover:border-slate-600';

/** Legacy-low treatment on the error chip when a stored value of 1 hydrates. */
const LEGACY_CLASS = 'bg-transparent border-red-800/60 text-red-400/80';

// ─────────────────────────────────────────────────────────────────────────────
// Component
// ─────────────────────────────────────────────────────────────────────────────

interface TurnRatingProps {
  /** Trace ID of the rated assistant turn — the ES join key (FRE-407). */
  traceId: string;
  /** Session that owns the turn — enforced server-side for ownership. */
  sessionId: string;
  /**
   * Previously-submitted 0–3 score, hydrated from history (FRE-426). Undefined
   * when the turn has no stored rating — the control then shows the resting
   * "ok" default (the persist-on-send write lands separately, from the DONE
   * hook for live turns and the one-time backfill for historical turns).
   */
  initialRating?: number;
}

/**
 * Three-chip per-turn rating control.
 *
 * Args:
 *   traceId:   Trace ID of the assistant turn to rate.
 *   sessionId: Owning session ID, forwarded to the rating endpoint.
 *   initialRating: Hydrated 0–3 store value, if any.
 */
export function TurnRating({ traceId, sessionId, initialRating }: TurnRatingProps) {
  /** Persisted store value (hydrated or set by the user); null = none stored. */
  const [persisted, setPersisted] = useState<number | null>(initialRating ?? null);
  /** Optimistic selection while a request is in-flight. */
  const [optimistic, setOptimistic] = useState<number | null>(null);
  /** Drives the pulse animation on successful confirmation. */
  const [pulsing, setPulsing] = useState<number | null>(null);
  const [submitting, setSubmitting] = useState(false);

  // Effective store value driving chip selection: optimistic > persisted > ok.
  const effective = optimistic ?? persisted ?? DEFAULT_OK;
  // A hydrated legacy "Low" (1) with no newer interaction renders its own state.
  const isLegacyLow = optimistic === null && persisted === LEGACY_LOW;

  const handleRate = useCallback(
    async (rating: number) => {
      if (submitting) return;

      const previous = persisted;
      // Optimistic paint.
      setOptimistic(rating);
      setSubmitting(true);

      const ok = await submitTurnRating(traceId, sessionId, rating);

      setSubmitting(false);
      setOptimistic(null);

      if (ok) {
        setPersisted(rating);
        setPulsing(rating);
        setTimeout(() => setPulsing(null), 800);
      } else {
        setPersisted(previous);
      }
    },
    [traceId, sessionId, persisted, submitting],
  );

  return (
    <div className="flex items-center gap-1" role="group" aria-label="Rate this response">
      {CHIPS.map((chip) => {
        const isSelected = !isLegacyLow && effective === chip.value;
        const showLegacy = isLegacyLow && chip.key === 'error';
        const isPulsing = pulsing !== null && isSelected && pulsing === chip.value;

        const stateClass = isSelected
          ? chip.selectedClass
          : showLegacy
            ? LEGACY_CLASS
            : UNSELECTED_CLASS;

        const label = showLegacy ? 'Legacy low rating — click to re-rate' : `Rate ${chip.label}`;

        return (
          <button
            key={chip.key}
            type="button"
            onClick={() => void handleRate(chip.value)}
            title={showLegacy ? 'Legacy low rating' : chip.label}
            aria-label={label}
            aria-pressed={isSelected}
            disabled={submitting}
            className={[
              // 44×44 hit target around a ~24px icon core (WCAG 2.5.5 / HIG).
              'flex h-11 w-11 items-center justify-center rounded-md border',
              'text-lg leading-none transition-colors disabled:cursor-not-allowed',
              stateClass,
              isPulsing ? 'animate-pulse' : '',
            ]
              .filter(Boolean)
              .join(' ')}
          >
            <span aria-hidden="true">{chip.glyph}</span>
          </button>
        );
      })}
    </div>
  );
}

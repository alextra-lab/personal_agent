/**
 * Seshat effort levels in the UI (ADR-0157 D1, D2, D5).
 */

import type { EffortLevelModes, SeshatEffort } from './types';

/** The levels in display order. */
export const EFFORT_LEVELS: readonly SeshatEffort[] = ['fast', 'standard', 'deep'];

/** The level a new session shows and sends (ADR-0157 D2: the owner's "standard"). */
export const DEFAULT_EFFORT: SeshatEffort = 'standard';

/** Shown when the selected model declares no mode for a level (ADR-0157 D2). */
export const MODEL_DEFAULT_LABEL = 'model default';

export const EFFORT_NAMES: Record<SeshatEffort, string> = {
  fast: 'Fast',
  standard: 'Standard',
  deep: 'Deep',
};

/**
 * The catalog mode that a level runs on the selected model (ADR-0157 D5).
 *
 * @param level         - The effort level.
 * @param levelModes    - The mode each level requests, from the config read.
 * @param declaredModes - The modes the selected `primary` deployment declares, or undefined
 *   when the deployment is not known.
 * @returns The mode name when the level requests one and the deployment declares it,
 *   {@link MODEL_DEFAULT_LABEL} when it does not, and null when the data is not loaded
 *   (the UI then shows no label instead of a guess).
 */
export function effortModeLabel(
  level: SeshatEffort,
  levelModes: EffortLevelModes | undefined,
  declaredModes: readonly string[] | undefined,
): string | null {
  if (!levelModes || !declaredModes) return null;
  const requested = levelModes[level];
  if (requested !== null && declaredModes.includes(requested)) return requested;
  return MODEL_DEFAULT_LABEL;
}

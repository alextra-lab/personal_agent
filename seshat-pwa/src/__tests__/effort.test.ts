/**
 * Tests for the effort-level labels (ADR-0157 D2, D5; FRE-1623 AC-3).
 */

import { describe, it, expect } from 'vitest';

import { MODEL_DEFAULT_LABEL, effortModeLabel } from '@/lib/effort';
import type { EffortLevelModes } from '@/lib/types';

const LEVEL_MODES: EffortLevelModes = { fast: 'fast', standard: null, deep: 'deep' };

describe('effortModeLabel', () => {
  it('names the mode the selected model declares, and "model default" when it declares none', () => {
    const model = ['default', 'fast'];
    expect(effortModeLabel('fast', LEVEL_MODES, model)).toBe('fast');
    expect(effortModeLabel('deep', LEVEL_MODES, model)).toBe(MODEL_DEFAULT_LABEL);
  });

  it('shows "model default" for Standard, which requests no mode', () => {
    expect(effortModeLabel('standard', LEVEL_MODES, ['default', 'fast'])).toBe(
      MODEL_DEFAULT_LABEL,
    );
  });

  it('shows "model default" when the model declares no mode at all', () => {
    expect(effortModeLabel('fast', LEVEL_MODES, [])).toBe(MODEL_DEFAULT_LABEL);
  });

  it('shows a deep mode once a model declares it', () => {
    expect(effortModeLabel('deep', LEVEL_MODES, ['default', 'deep'])).toBe('deep');
  });

  it('shows no label when the modes are unknown, never a guess', () => {
    expect(effortModeLabel('fast', LEVEL_MODES, undefined)).toBeNull();
    expect(effortModeLabel('fast', undefined, ['fast'])).toBeNull();
  });
});

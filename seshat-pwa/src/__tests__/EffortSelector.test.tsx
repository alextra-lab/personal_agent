/**
 * Tests for EffortSelector (ADR-0157 D2, D5; FRE-1623 AC-2 and AC-3).
 */

import { render, screen, fireEvent, within } from '@testing-library/react';
import { vi, describe, it, expect } from 'vitest';

import { EffortSelector } from '@/components/EffortSelector';
import type { EffortLevelModes } from '@/lib/types';

const LEVEL_MODES: EffortLevelModes = { fast: 'fast', standard: null, deep: 'deep' };

// The session's model declares `fast` and no `deep` (AC-3).
const DECLARED = ['default', 'fast'];

function renderSelector(overrides: Partial<Parameters<typeof EffortSelector>[0]> = {}) {
  const onSelect = vi.fn();
  render(
    <EffortSelector
      level="standard"
      levelModes={LEVEL_MODES}
      declaredModes={DECLARED}
      error={null}
      onSelect={onSelect}
      {...overrides}
    />,
  );
  return { onSelect };
}

function open() {
  fireEvent.click(screen.getByLabelText('Choose Seshat effort'));
  return screen.getByRole('listbox');
}

describe('EffortSelector — the control', () => {
  it('shows the current level on the button', () => {
    renderSelector({ level: 'deep' });
    expect(screen.getByLabelText('Choose Seshat effort')).toHaveTextContent('Deep');
  });

  it('shows a neutral label until the server has answered', () => {
    renderSelector({ level: null });
    expect(screen.getByLabelText('Choose Seshat effort')).toHaveTextContent('Effort');
  });

  it('lists Fast, Standard and Deep and marks the current one', () => {
    renderSelector({ level: 'standard' });
    const options = within(open()).getAllByRole('option');
    expect(options.map((o) => o.textContent)).toEqual([
      expect.stringContaining('Fast'),
      expect.stringContaining('Standard'),
      expect.stringContaining('Deep'),
    ]);
    expect(options[1]).toHaveAttribute('aria-selected', 'true');
    expect(options[0]).toHaveAttribute('aria-selected', 'false');
  });

  it('reports the chosen level and closes the list', () => {
    const { onSelect } = renderSelector();
    fireEvent.click(within(open()).getByRole('option', { name: /Deep/ }));
    expect(onSelect).toHaveBeenCalledTimes(1);
    expect(onSelect).toHaveBeenCalledWith('deep');
    expect(screen.queryByRole('listbox')).toBeNull();
  });
});

describe('EffortSelector — AC-3: the mode of each level', () => {
  it('shows the fast mode for Fast and "model default" for Deep', () => {
    renderSelector();
    const list = open();
    expect(within(list).getByRole('option', { name: /Fast/ })).toHaveTextContent('fast');
    expect(within(list).getByRole('option', { name: /Deep/ })).toHaveTextContent('model default');
    expect(within(list).getByRole('option', { name: /Deep/ })).not.toHaveTextContent(/\bdeep mode\b/);
  });

  it('shows "model default" for Standard, which requests no mode', () => {
    renderSelector();
    expect(within(open()).getByRole('option', { name: /Standard/ })).toHaveTextContent(
      'model default',
    );
  });

  it('shows a deep mode once the model declares it', () => {
    renderSelector({ declaredModes: ['default', 'deep'] });
    const deep = within(open()).getByRole('option', { name: /Deep/ });
    expect(deep).toHaveTextContent('deep');
    expect(deep).not.toHaveTextContent('model default');
  });

  it('shows no mode text when the modes are not loaded', () => {
    renderSelector({ levelModes: undefined, declaredModes: undefined });
    const list = open();
    expect(list).not.toHaveTextContent('model default');
  });
});

describe('EffortSelector — AC-2: the error', () => {
  it('shows the error of a failed write', () => {
    renderSelector({ error: 'setSessionEffort failed: 500' });
    expect(screen.getByRole('alert')).toHaveTextContent('setSessionEffort failed: 500');
  });

  it('shows no alert without an error', () => {
    renderSelector();
    expect(screen.queryByRole('alert')).toBeNull();
  });
});

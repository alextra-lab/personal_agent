/**
 * Tests for ModelPicker (ADR-0121 §3, FRE-920) — replaces the profile pill.
 */

import { render, screen, fireEvent } from '@testing-library/react';
import { vi, describe, it, expect } from 'vitest';

import { ModelPicker } from '@/components/ModelPicker';
import type { DeploymentView } from '@/lib/types';

const LOCAL_MODEL: DeploymentView = {
  key: 'qwen3.6-35b-thinking',
  id: 'unsloth/qwen3.6-35-A3B',
  provider: 'slm_local',
  placement: 'local',
  kind: 'llm',
  status: 'active',
  summary: 'Local Qwen — private, no cost.',
  context_length: 131072,
  max_tokens: 8192,
  supports_vision: true,
  supports_pdf_document: false,
  input_cost_per_token: null,
  output_cost_per_token: null,
};

const CLOUD_MODEL: DeploymentView = {
  key: 'claude_sonnet',
  id: 'claude-sonnet-5',
  provider: 'anthropic',
  placement: 'cloud',
  kind: 'llm',
  status: 'active',
  summary: 'Claude Sonnet — fast, capable.',
  context_length: 200000,
  max_tokens: 32768,
  supports_vision: true,
  supports_pdf_document: true,
  input_cost_per_token: 0.000003,
  output_cost_per_token: 0.000015,
};

const CANDIDATES = [LOCAL_MODEL, CLOUD_MODEL];

describe('ModelPicker — closed state', () => {
  it('shows the selected model key as the button label', () => {
    render(
      <ModelPicker
        candidates={CANDIDATES}
        selectedKey="claude_sonnet"
        hydrated={true}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.getByLabelText('Choose model')).toHaveTextContent('claude_sonnet');
  });

  it('shows a placeholder label before hydration', () => {
    render(
      <ModelPicker candidates={CANDIDATES} selectedKey={null} hydrated={false} onSelect={vi.fn()} />,
    );
    expect(screen.getByLabelText('Choose model')).toHaveTextContent('Model');
  });

  it('does not render the candidate list until opened', () => {
    render(
      <ModelPicker
        candidates={CANDIDATES}
        selectedKey="claude_sonnet"
        hydrated={true}
        onSelect={vi.fn()}
      />,
    );
    expect(screen.queryByRole('listbox')).toBeNull();
  });
});

describe('ModelPicker — open state', () => {
  it('opens the candidate list on click and shows every candidate', () => {
    render(
      <ModelPicker
        candidates={CANDIDATES}
        selectedKey="claude_sonnet"
        hydrated={true}
        onSelect={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByLabelText('Choose model'));
    const listbox = screen.getByRole('listbox');
    expect(listbox).toBeDefined();
    expect(screen.getByText('qwen3.6-35b-thinking')).toBeDefined();
    // "claude_sonnet" also appears in the closed-button label — scope to the list.
    expect(screen.getAllByText('claude_sonnet')).toHaveLength(2);
  });

  it('marks the currently selected candidate', () => {
    render(
      <ModelPicker
        candidates={CANDIDATES}
        selectedKey="claude_sonnet"
        hydrated={true}
        onSelect={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByLabelText('Choose model'));
    const options = screen.getAllByRole('option');
    const selected = options.find((o) => o.getAttribute('aria-selected') === 'true');
    expect(selected).toHaveTextContent('claude_sonnet');
  });

  it('calls onSelect with the clicked candidate key and closes the list', () => {
    const onSelect = vi.fn();
    render(
      <ModelPicker candidates={CANDIDATES} selectedKey="claude_sonnet" hydrated={true} onSelect={onSelect} />,
    );
    fireEvent.click(screen.getByLabelText('Choose model'));
    fireEvent.click(screen.getByText('qwen3.6-35b-thinking'));

    expect(onSelect).toHaveBeenCalledWith('qwen3.6-35b-thinking');
    expect(screen.queryByRole('listbox')).toBeNull();
  });

  it('shows an empty state when there are no candidates', () => {
    render(<ModelPicker candidates={[]} selectedKey={null} hydrated={true} onSelect={vi.fn()} />);
    fireEvent.click(screen.getByLabelText('Choose model'));
    expect(screen.getByText('No models available')).toBeDefined();
  });
});

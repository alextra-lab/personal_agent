/**
 * Tests for ApprovalModal (ADR-0156 D8.3, FRE-1574): the card shows the tool, the
 * exact arguments and the worker, and offers "Allow for this session" only when the
 * backend offers it.
 */

import { render, screen, fireEvent } from '@testing-library/react';
import { vi, describe, it, expect } from 'vitest';

import { ApprovalModal } from '@/components/ApprovalModal';
import type { ToolApprovalRequestData } from '@/lib/types';

function request(overrides: Partial<ToolApprovalRequestData> = {}): ToolApprovalRequestData {
  return {
    request_id: 'req-1574',
    trace_id: 'trace-1574',
    tool: 'bash',
    args: { command: 'touch /tmp/marker' },
    risk_level: 'high',
    reason: 'no allow rule matches this subcommand: touch /tmp/marker',
    expires_at: new Date(Date.now() + 60_000).toISOString(),
    worker: null,
    session_option: true,
    ...overrides,
  };
}

function renderModal(data: ToolApprovalRequestData) {
  const handlers = { onApprove: vi.fn(), onApproveSession: vi.fn(), onDeny: vi.fn() };
  render(<ApprovalModal data={data} {...handlers} />);
  return handlers;
}

describe('ApprovalModal', () => {
  it('shows the tool and the exact arguments without a click', () => {
    renderModal(request());
    expect(screen.getByText('bash')).toBeInTheDocument();
    expect(screen.getByLabelText('Tool arguments').textContent).toContain('touch /tmp/marker');
  });

  it('names the worker when a worker asks', () => {
    renderModal(request({ worker: 'general' }));
    expect(screen.getByText('general')).toBeInTheDocument();
  });

  it('shows no worker line for the primary', () => {
    renderModal(request());
    expect(screen.queryByText(/Asked by worker/)).not.toBeInTheDocument();
  });

  it('sends each answer to its own handler', () => {
    const handlers = renderModal(request());
    fireEvent.click(screen.getByRole('button', { name: 'Allow once' }));
    fireEvent.click(screen.getByRole('button', { name: 'Allow for this session' }));
    fireEvent.click(screen.getByRole('button', { name: 'Deny' }));
    expect(handlers.onApprove).toHaveBeenCalledTimes(1);
    expect(handlers.onApproveSession).toHaveBeenCalledTimes(1);
    expect(handlers.onDeny).toHaveBeenCalledTimes(1);
  });

  it('offers no session answer for an explicit ask rule', () => {
    renderModal(request({ session_option: false }));
    expect(screen.queryByRole('button', { name: 'Allow for this session' })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Allow once' })).toBeInTheDocument();
  });
});

'use client';

import type { TurnStatus } from '@/lib/types';

export interface TurnStatusBarProps {
  status: TurnStatus | null;
}

function formatTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}K`;
  return String(n);
}

/**
 * Persistent per-turn status bar (ADR-0076), shown during active streaming.
 *
 * Replaces ContextBudgetMeter. Surfaces live context-window usage, tool
 * iteration count, and accrued cost from `turn_status` STATE_DELTA events.
 * Colour thresholds: context amber ≥70% / red ≥85%; tools amber at max−2.
 */
export function TurnStatusBar({ status }: TurnStatusBarProps) {
  if (status === null) return null;

  const { context_tokens, context_max, tool_iteration, tool_iteration_max, turn_cost_usd } =
    status;

  const ctxPct = context_max > 0 ? Math.round((context_tokens / context_max) * 100) : 0;
  const ctxBar =
    ctxPct >= 85 ? 'bg-red-500' : ctxPct >= 70 ? 'bg-amber-500' : 'bg-emerald-500';
  const ctxLabel =
    ctxPct >= 85 ? 'text-red-400' : ctxPct >= 70 ? 'text-amber-400' : 'text-emerald-400';

  const toolsAmber = tool_iteration_max > 0 && tool_iteration >= tool_iteration_max - 2;
  const toolsColor = toolsAmber ? 'text-amber-400' : 'text-slate-400';

  return (
    <div className="px-4 py-1.5 flex items-center gap-3 text-xs">
      <span className="text-slate-500 flex-shrink-0">ctx</span>
      <div className="flex items-center gap-2 flex-1 min-w-0">
        <div className="flex-1 h-1 bg-slate-700 rounded-full overflow-hidden">
          <div
            className={`h-full rounded-full transition-all duration-300 ${ctxBar}`}
            style={{ width: `${Math.min(ctxPct, 100)}%` }}
          />
        </div>
        <span className={`font-mono flex-shrink-0 ${ctxLabel}`}>
          {formatTokens(context_tokens)}/{formatTokens(context_max)} {ctxPct}%
        </span>
      </div>
      <span className={`font-mono flex-shrink-0 ${toolsColor}`}>
        tools {tool_iteration}/{tool_iteration_max}
      </span>
      <span className="font-mono flex-shrink-0 text-slate-400">
        ${turn_cost_usd.toFixed(2)}
      </span>
    </div>
  );
}

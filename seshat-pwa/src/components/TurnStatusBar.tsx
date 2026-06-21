'use client';

import type { TurnStatus } from '@/lib/types';

export interface TurnStatusBarProps {
  status: TurnStatus | null;
}

function formatTokens(n: number): string {
  if (n >= 1000) return `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}K`;
  return String(n);
}

function safeNum(v: number | undefined | null): number {
  return typeof v === 'number' && isFinite(v) ? v : 0;
}

/**
 * Two-lane persistent status bar (ADR-0092 §D9).
 *
 * Session lane (top): cumulative cost, context occupancy, ⟳ compression
 * count, ↻ cache-reset count, ⚠ quality alert.
 * Engagement lane (bottom): live tool iteration X/Y (FRE-553).
 *
 * Colour thresholds: context amber ≥70% / red ≥85%; tools amber at max−2.
 * Mounted persistently in the footer near ChatInput (ADR-0092 §D9 / PWA
 * persistent-status convention).
 */
export function TurnStatusBar({ status }: TurnStatusBarProps) {
  if (status === null) return null;

  // --- Session lane ---
  const sessionCost = safeNum(status.session_cost_usd);
  const sessionCtxTokens = safeNum(status.session_context_tokens);
  const ctxMax = safeNum(status.context_max);
  const compactionCount = safeNum(status.compaction_count);
  const cacheResetCount = safeNum(status.cache_reset_count);
  const qualityAlert = status.quality_alert ?? null;

  const ctxPct = ctxMax > 0 ? Math.round((sessionCtxTokens / ctxMax) * 100) : 0;
  const ctxBar =
    ctxPct >= 85 ? 'bg-red-500' : ctxPct >= 70 ? 'bg-amber-500' : 'bg-emerald-500';
  const ctxLabel =
    ctxPct >= 85 ? 'text-red-400' : ctxPct >= 70 ? 'text-amber-400' : 'text-emerald-400';

  const alertColor =
    qualityAlert?.severity === 'high' ? 'text-red-400' : 'text-amber-400';

  // --- Engagement lane ---
  const toolIter = safeNum(status.tool_iteration);
  const toolIterMax = safeNum(status.tool_iteration_max);
  const toolsAmber = toolIterMax > 0 && toolIter >= toolIterMax - 2;
  const toolsColor = toolsAmber ? 'text-amber-400' : 'text-slate-400';

  return (
    <div className="px-4 py-1.5 flex flex-col gap-0.5 text-xs">
      {/* Session lane */}
      <div className="flex items-center gap-3">
        <span className="text-slate-500 flex-shrink-0">ctx</span>
        <div className="flex items-center gap-2 flex-1 min-w-0">
          <div className="flex-1 h-1 bg-slate-700 rounded-full overflow-hidden">
            <div
              className={`h-full rounded-full transition-all duration-300 ${ctxBar}`}
              style={{ width: `${Math.min(ctxPct, 100)}%` }}
            />
          </div>
          <span className={`font-mono flex-shrink-0 ${ctxLabel}`}>
            {formatTokens(sessionCtxTokens)}/{formatTokens(ctxMax)} {ctxPct}%
          </span>
        </div>
        <span className="font-mono flex-shrink-0 text-slate-400">
          ${sessionCost.toFixed(2)}
        </span>
        {compactionCount > 0 && (
          <span className="font-mono flex-shrink-0 text-slate-400">
            ⟳{compactionCount}
          </span>
        )}
        {cacheResetCount > 0 && (
          <span className="font-mono flex-shrink-0 text-slate-400">
            ↻{cacheResetCount}
          </span>
        )}
        {qualityAlert !== null && (
          <span className={`font-mono flex-shrink-0 font-semibold ${alertColor}`}>
            ⚠
          </span>
        )}
      </div>
      {/* Engagement lane */}
      <div className="flex items-center gap-3">
        <span className={`font-mono flex-shrink-0 ${toolsColor}`}>
          tools {toolIter}/{toolIterMax}
        </span>
      </div>
    </div>
  );
}

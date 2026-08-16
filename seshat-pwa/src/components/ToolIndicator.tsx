'use client';

import { useState } from 'react';

import type { ToolCall } from '@/lib/types';

interface ToolIndicatorProps {
  tools: ToolCall[];
}

function ToolRow({ tool }: { tool: ToolCall }) {
  return (
    <div className="flex items-center gap-2 text-xs text-ink-muted">
      {tool.status === 'running' ? (
        <span className="flex items-center gap-1.5">
          {/* Animated spinner */}
          <svg
            className="animate-spin h-3.5 w-3.5 text-amber-400 flex-shrink-0"
            xmlns="http://www.w3.org/2000/svg"
            fill="none"
            viewBox="0 0 24 24"
          >
            <circle
              className="opacity-25"
              cx="12"
              cy="12"
              r="10"
              stroke="currentColor"
              strokeWidth="4"
            />
            <path
              className="opacity-75"
              fill="currentColor"
              d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"
            />
          </svg>
          <span className="font-mono text-amber-400">{tool.name}</span>
          <span>running...</span>
        </span>
      ) : (
        <span className="flex items-center gap-1.5">
          {/* Checkmark */}
          <svg
            className="h-3.5 w-3.5 text-emerald-400 flex-shrink-0"
            xmlns="http://www.w3.org/2000/svg"
            viewBox="0 0 20 20"
            fill="currentColor"
          >
            <path
              fillRule="evenodd"
              d="M16.704 4.153a.75.75 0 01.143 1.052l-8 10.5a.75.75 0 01-1.127.075l-4.5-4.5a.75.75 0 011.06-1.06l3.894 3.893 7.48-9.817a.75.75 0 011.05-.143z"
              clipRule="evenodd"
            />
          </svg>
          <span className="font-mono text-emerald-400">{tool.name}</span>
          {tool.result && (
            <span className="text-ink-muted truncate max-w-[200px]">
              — {tool.result}
            </span>
          )}
        </span>
      )}
    </div>
  );
}

function ChevronIcon({ expanded }: { expanded: boolean }) {
  return (
    <svg
      className={`h-3 w-3 flex-shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 20 20"
      fill="currentColor"
    >
      <path
        fillRule="evenodd"
        d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z"
        clipRule="evenodd"
      />
    </svg>
  );
}

/**
 * Displays active tool calls with a spinner for running tools and a
 * checkmark for completed tools.
 *
 * Renders nothing when the tools array is empty. A single tool renders
 * directly; more than one collapses behind a summary line by default
 * (FRE-1264 AC-6) — the badge row no longer renders fully expanded for a
 * multi-tool turn.
 */
export function ToolIndicator({ tools }: ToolIndicatorProps) {
  const [expanded, setExpanded] = useState(false);

  if (tools.length === 0) return null;

  if (tools.length === 1) {
    return (
      <div className="px-4 py-2">
        <ToolRow tool={tools[0]} />
      </div>
    );
  }

  const allDone = tools.every((t) => t.status === 'completed');

  return (
    <div className="px-4 py-2">
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        aria-expanded={expanded}
        className="flex items-center gap-1.5 text-xs text-ink-muted hover:text-ink transition-colors"
      >
        <ChevronIcon expanded={expanded} />
        {allDone ? `Used ${tools.length} tools` : `Using ${tools.length} tools…`}
      </button>
      {expanded && (
        <div className="mt-1.5 flex flex-col gap-1.5 pl-4">
          {tools.map((tool) => (
            <ToolRow key={`${tool.name}-${tool.status}`} tool={tool} />
          ))}
        </div>
      )}
    </div>
  );
}

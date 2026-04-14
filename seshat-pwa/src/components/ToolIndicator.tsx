'use client';

import type { ToolCall } from '@/lib/types';

interface ToolIndicatorProps {
  tools: ToolCall[];
}

/**
 * Displays active tool calls with a spinner for running tools and a
 * checkmark for completed tools.
 *
 * Renders nothing when the tools array is empty.
 */
export function ToolIndicator({ tools }: ToolIndicatorProps) {
  if (tools.length === 0) return null;

  return (
    <div className="px-4 py-2 flex flex-col gap-1.5">
      {tools.map((tool) => (
        <div
          key={`${tool.name}-${tool.status}`}
          className="flex items-center gap-2 text-xs text-slate-400"
        >
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
                <span className="text-slate-500 truncate max-w-[200px]">
                  — {tool.result}
                </span>
              )}
            </span>
          )}
        </div>
      ))}
    </div>
  );
}

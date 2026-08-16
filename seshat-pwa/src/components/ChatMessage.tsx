'use client';

import { useState } from 'react';

import type { ChatMessage as ChatMessageType, ToolCall } from '@/lib/types';
import { MarkdownContent } from './MarkdownContent';
import { TurnRating } from './TurnRating';
import { TurnSummaryPanel } from './TurnSummaryPanel';

interface ChatMessageProps {
  message: ChatMessageType;
  /** Session ID forwarded to TurnRating for ownership scoping (FRE-407). */
  sessionId?: string;
}

function ToolCallBadge({ tool }: { tool: ToolCall }) {
  return (
    <span
      className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full font-mono ${
        tool.status === 'running'
          ? 'bg-amber-900/40 text-amber-300 border border-amber-700/50'
          : 'bg-emerald-900/40 text-emerald-300 border border-emerald-700/50'
      }`}
    >
      {tool.status === 'running' ? (
        <span className="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse" />
      ) : (
        <span className="text-emerald-400">&#10003;</span>
      )}
      {tool.name}
    </span>
  );
}

function CopyButton({ content }: { content: string }) {
  const [copied, setCopied] = useState(false);

  const handleCopy = async () => {
    try {
      await navigator.clipboard.writeText(content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // clipboard API unavailable — silent fail
    }
  };

  return (
    <button
      onClick={handleCopy}
      className="opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity text-ink-muted hover:text-ink p-1 rounded"
      aria-label="Copy message"
    >
      {copied ? (
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor" className="w-3.5 h-3.5 text-emerald-400">
          <path fillRule="evenodd" d="M12.416 3.376a.75.75 0 0 1 .208 1.04l-5 7.5a.75.75 0 0 1-1.154.114l-3-3a.75.75 0 0 1 1.06-1.06l2.353 2.353 4.431-6.647a.75.75 0 0 1 1.102-.3Z" clipRule="evenodd" />
        </svg>
      ) : (
        <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16" fill="currentColor" className="w-3.5 h-3.5">
          <path d="M5.5 3.5A1.5 1.5 0 0 1 7 2h2.879a1.5 1.5 0 0 1 1.06.44l2.122 2.12a1.5 1.5 0 0 1 .439 1.061V9.5A1.5 1.5 0 0 1 12 11H9.5a.5.5 0 0 1 0-1H12a.5.5 0 0 0 .5-.5V6H10a1 1 0 0 1-1-1V2.5H7a.5.5 0 0 0-.5.5v1a.5.5 0 0 1-1 0V3.5Z" />
          <path d="M4.5 6a1.5 1.5 0 0 0-1.5 1.5v5A1.5 1.5 0 0 0 4.5 14h3A1.5 1.5 0 0 0 9 12.5v-5A1.5 1.5 0 0 0 7.5 6h-3Zm0 1h3a.5.5 0 0 1 .5.5v5a.5.5 0 0 1-.5.5h-3a.5.5 0 0 1-.5-.5v-5a.5.5 0 0 1 .5-.5Z" />
        </svg>
      )}
    </button>
  );
}

/**
 * Renders a single chat message (FRE-1264 — alignment and typeface, not
 * chrome, carry the role distinction; no avatar, no role label).
 *
 * User messages render as a right-aligned bubble capped at ~75% width.
 * Assistant messages render full-width serif markdown with no bubble.
 */
export function ChatMessage({ message, sessionId }: ChatMessageProps) {
  const isUser = message.role === 'user';

  // Gate: show TurnRating only for completed assistant turns with a traceId.
  // Never renders mid-stream (complete is only set on DONE) or for user messages.
  const showRating =
    !isUser &&
    message.complete === true &&
    typeof message.traceId === 'string' &&
    message.traceId.length > 0 &&
    typeof sessionId === 'string' &&
    sessionId.length > 0;

  // A placeholder row (e.g. a turn cancelled before any output) has nothing
  // meaningful to copy (ADR-0123 T4, FRE-937).
  const hasCopyable = message.content.length > 0;

  const controls = (
    <div className="mt-1.5 flex items-center gap-2">
      <span className="text-xs text-ink-muted">
        {message.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
      </span>
      {/* TurnRating is persistently visible by design (FRE-757) — never hover-gated. */}
      {showRating && (
        <TurnRating
          traceId={message.traceId!}
          sessionId={sessionId!}
          initialRating={message.rating}
        />
      )}
      {hasCopyable && <CopyButton content={message.content} />}
    </div>
  );

  if (isUser) {
    return (
      <div className="group px-4 py-2 flex justify-end">
        {/* Visually hidden — AC-5 removes the *visible* avatar/role label
            (alignment + typeface carry the distinction for sighted users),
            but a screen-reader user still needs a cue who's speaking. */}
        <span className="sr-only">You said</span>
        <div className="max-w-[75%] flex flex-col items-end">
          <div className="rounded-2xl bg-surface border border-line px-4 py-2.5">
            <p className="text-sm leading-relaxed whitespace-pre-wrap break-words text-ink">
              {message.content}
            </p>
          </div>
          {controls}
        </div>
      </div>
    );
  }

  return (
    <div className="group px-4 py-4">
      <span className="sr-only">Seshat said</span>
      <MarkdownContent content={message.content} />

      {/* Tool call badges — message.toolCalls is not populated by the live
          stream today (useSSEStream routes tool state through activeTools /
          phaseSummary.tools instead); kept for a hydration/history shape
          that does carry it. */}
      {message.toolCalls && message.toolCalls.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {message.toolCalls.map((tool) => (
            <ToolCallBadge key={`${tool.name}-${tool.status}`} tool={tool} />
          ))}
        </div>
      )}

      {/* Collapsed per-turn summary (ADR-0123 T4, FRE-937) */}
      <TurnSummaryPanel summary={message.phaseSummary} />

      {controls}
    </div>
  );
}

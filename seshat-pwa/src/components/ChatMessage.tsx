'use client';

import { useState } from 'react';

import type { ChatMessage as ChatMessageType, ToolCall } from '@/lib/types';
import { MarkdownContent } from './MarkdownContent';

interface ChatMessageProps {
  message: ChatMessageType;
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
      className="opacity-0 group-hover:opacity-100 focus:opacity-100 transition-opacity text-slate-500 hover:text-slate-300 p-1 rounded"
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
 * Renders a single chat message in full-width layout.
 *
 * Inspired by Claude.ai: avatar initial, role label, full-width content,
 * and an icon copy button that appears on hover.
 *
 * User messages have a subtle background tint for visual distinction.
 * Assistant messages render markdown with syntax-highlighted code blocks.
 */
export function ChatMessage({ message }: ChatMessageProps) {
  const isUser = message.role === 'user';

  return (
    <div className={`group px-4 py-5 border-b border-slate-800/60 ${isUser ? 'bg-slate-800/40' : ''}`}>
      {/* Avatar + role row */}
      <div className="flex items-center gap-2.5 mb-2">
        <div
          className={`w-6 h-6 rounded-full flex items-center justify-center text-xs font-semibold flex-shrink-0 ${
            isUser
              ? 'bg-blue-600 text-white'
              : 'bg-orange-500/20 text-orange-400 border border-orange-500/30'
          }`}
        >
          {isUser ? 'Y' : 'S'}
        </div>
        <span className={`text-xs font-semibold ${isUser ? 'text-slate-300' : 'text-slate-400'}`}>
          {isUser ? 'You' : 'Seshat'}
        </span>
        <span className="text-xs text-slate-600 ml-auto">
          {message.timestamp.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
        </span>
        <CopyButton content={message.content} />
      </div>

      {/* Content */}
      <div className="pl-[34px]">
        {isUser ? (
          <p className="text-sm leading-relaxed whitespace-pre-wrap break-words text-slate-100">
            {message.content}
          </p>
        ) : (
          <MarkdownContent content={message.content} />
        )}

        {/* Tool call badges */}
        {!isUser && message.toolCalls && message.toolCalls.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1">
            {message.toolCalls.map((tool) => (
              <ToolCallBadge key={`${tool.name}-${tool.status}`} tool={tool} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

'use client';

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

/**
 * Renders a single chat message bubble.
 *
 * User messages appear on the right in blue as plain text.
 * Assistant messages appear on the left in slate with markdown rendering.
 */
export function ChatMessage({ message }: ChatMessageProps) {
  const isUser = message.role === 'user';

  return (
    <div className={`flex ${isUser ? 'justify-end' : 'justify-start'} mb-4`}>
      <div
        className={`max-w-[80%] px-4 py-3 rounded-2xl ${
          isUser
            ? 'bg-blue-600 text-white rounded-br-sm'
            : 'bg-slate-700 text-slate-100 rounded-bl-sm'
        }`}
      >
        {isUser ? (
          // User messages: plain text, preserve whitespace
          <p className="text-sm leading-relaxed whitespace-pre-wrap break-words">
            {message.content}
          </p>
        ) : (
          // Assistant messages: full markdown rendering
          <MarkdownContent content={message.content} />
        )}

        {/* Tool call badges for assistant messages */}
        {!isUser && message.toolCalls && message.toolCalls.length > 0 && (
          <div className="mt-2 flex flex-wrap gap-1">
            {message.toolCalls.map((tool) => (
              <ToolCallBadge key={`${tool.name}-${tool.status}`} tool={tool} />
            ))}
          </div>
        )}

        {/* Timestamp */}
        <p
          className={`text-xs mt-1 ${
            isUser ? 'text-blue-200' : 'text-slate-400'
          }`}
        >
          {message.timestamp.toLocaleTimeString([], {
            hour: '2-digit',
            minute: '2-digit',
          })}
        </p>
      </div>
    </div>
  );
}

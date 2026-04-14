'use client';

import { useState, useRef, type KeyboardEvent, type FormEvent } from 'react';

interface ChatInputProps {
  onSend: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
}

/**
 * Chat input bar with a textarea and send button.
 *
 * Behaviour:
 * - Enter sends the message; Shift+Enter inserts a newline.
 * - The textarea auto-grows up to 5 lines.
 * - The send button and keyboard shortcut are disabled while streaming.
 */
export function ChatInput({
  onSend,
  disabled = false,
  placeholder = 'Message Seshat...',
}: ChatInputProps) {
  const [text, setText] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSubmit = (e?: FormEvent) => {
    e?.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText('');
    // Reset textarea height after clearing.
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit();
    }
  };

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value);
    // Auto-grow textarea.
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`; // max ~5 lines
  };

  const canSend = text.trim().length > 0 && !disabled;

  return (
    <form
      onSubmit={handleSubmit}
      className="flex items-end gap-2 px-4 py-3 border-t border-slate-700 bg-slate-900"
    >
      <textarea
        ref={textareaRef}
        value={text}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        placeholder={disabled ? 'Seshat is thinking...' : placeholder}
        disabled={disabled}
        rows={1}
        className={`
          flex-1 resize-none rounded-xl px-4 py-3 text-sm
          bg-slate-800 border border-slate-600
          text-slate-100 placeholder-slate-500
          focus:outline-none focus:border-blue-500 focus:ring-1 focus:ring-blue-500/50
          disabled:opacity-50 disabled:cursor-not-allowed
          transition-colors duration-150
          min-h-[44px] max-h-[140px]
        `}
      />
      <button
        type="submit"
        disabled={!canSend}
        aria-label="Send message"
        className={`
          flex-shrink-0 w-10 h-10 rounded-xl flex items-center justify-center
          transition-all duration-150
          ${
            canSend
              ? 'bg-blue-600 hover:bg-blue-500 text-white cursor-pointer'
              : 'bg-slate-700 text-slate-500 cursor-not-allowed'
          }
        `}
      >
        {/* Arrow-up icon */}
        <svg
          xmlns="http://www.w3.org/2000/svg"
          viewBox="0 0 20 20"
          fill="currentColor"
          className="w-5 h-5"
        >
          <path
            fillRule="evenodd"
            d="M10 17a.75.75 0 01-.75-.75V5.612L5.29 9.77a.75.75 0 01-1.08-1.04l5.25-5.5a.75.75 0 011.08 0l5.25 5.5a.75.75 0 11-1.08 1.04L10.75 5.612V16.25A.75.75 0 0110 17z"
            clipRule="evenodd"
          />
        </svg>
      </button>
    </form>
  );
}

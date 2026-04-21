'use client';

import { useState, useRef, type KeyboardEvent, type FormEvent, type ClipboardEvent } from 'react';

import type { ExecutionProfile } from '@/lib/types';

interface ChatInputProps {
  onSend: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
  profile: ExecutionProfile;
  onProfileChange: (profile: ExecutionProfile) => void;
}

/**
 * Chat input bar with textarea, inline model toggle, and send button.
 *
 * Behaviour:
 * - Desktop: Enter sends; Shift+Enter inserts newline.
 * - Mobile (touch device): Enter always inserts newline; send requires button tap.
 * - Paste is normalised to plain text.
 * - The textarea auto-grows up to 5 lines.
 * - Footer padding accounts for iOS home-indicator via safe-area-inset-bottom.
 */
export function ChatInput({
  onSend,
  disabled = false,
  placeholder = 'Message Seshat...',
  profile,
  onProfileChange,
}: ChatInputProps) {
  const [text, setText] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleSubmit = (e?: FormEvent) => {
    e?.preventDefault();
    const trimmed = text.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setText('');
    if (textareaRef.current) {
      textareaRef.current.style.height = 'auto';
    }
  };

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key !== 'Enter' || e.shiftKey) return;
    const isTouchDevice = typeof navigator !== 'undefined' && navigator.maxTouchPoints > 0;
    if (isTouchDevice) return; // mobile Enter = newline, send via button only
    e.preventDefault();
    handleSubmit();
  };

  const handleChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    setText(e.target.value);
    const el = e.target;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
  };

  const handlePaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    e.preventDefault();
    const plain = e.clipboardData.getData('text/plain');
    const el = textareaRef.current;
    if (!el) return;
    const start = el.selectionStart;
    const end = el.selectionEnd;
    const newValue = text.slice(0, start) + plain + text.slice(end);
    setText(newValue);
    // Restore cursor after state update.
    requestAnimationFrame(() => {
      el.selectionStart = el.selectionEnd = start + plain.length;
      el.style.height = 'auto';
      el.style.height = `${Math.min(el.scrollHeight, 140)}px`;
    });
  };

  const toggleProfile = () => {
    onProfileChange(profile === 'local' ? 'cloud' : 'local');
  };

  const canSend = text.trim().length > 0 && !disabled;

  return (
    <form
      onSubmit={handleSubmit}
      className="flex items-end gap-2 px-4 pt-3 border-t border-slate-800 bg-slate-900"
      style={{ paddingBottom: 'calc(env(safe-area-inset-bottom, 0px) + 0.75rem)' }}
    >
      {/* Compact model toggle — colored dot + label */}
      <button
        type="button"
        onClick={toggleProfile}
        disabled={disabled}
        className="flex-shrink-0 self-end mb-[9px] flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg bg-slate-800 border border-slate-700 text-slate-400 hover:border-slate-500 hover:text-slate-200 disabled:opacity-40 transition-colors whitespace-nowrap"
        title={profile === 'local' ? 'Switch to Cloud (Claude Sonnet)' : 'Switch to Local (Qwen)'}
      >
        <span
          className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
            profile === 'local' ? 'bg-emerald-400' : 'bg-amber-400'
          }`}
        />
        {profile === 'local' ? 'Local' : 'Cloud'}
      </button>

      <textarea
        ref={textareaRef}
        value={text}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        onPaste={handlePaste}
        placeholder={disabled ? 'Seshat is thinking...' : placeholder}
        disabled={disabled}
        rows={1}
        className={`
          flex-1 resize-none rounded-2xl px-4 py-3 text-sm
          bg-slate-800 border border-slate-700
          text-slate-100 placeholder-slate-500
          focus:outline-none focus:border-slate-500 focus:ring-1 focus:ring-slate-500/30
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
          flex-shrink-0 w-9 h-9 rounded-full flex items-center justify-center
          transition-all duration-150
          ${
            canSend
              ? 'bg-white hover:bg-slate-100 text-slate-900 cursor-pointer'
              : 'bg-slate-700 text-slate-500 cursor-not-allowed'
          }
        `}
      >
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

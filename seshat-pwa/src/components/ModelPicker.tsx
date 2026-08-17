'use client';

import { useState, useRef, useEffect } from 'react';

import type { DeploymentView } from '@/lib/types';

interface ModelPickerProps {
  /** The `primary` role's selectable candidates (already availability-filtered server-side). */
  candidates: DeploymentView[];
  /** The currently resolved/selected deployment key, or null before the config loads. */
  selectedKey: string | null;
  /** False while only the sessionless fallback has loaded — selectedKey may be stale. */
  hydrated: boolean;
  onSelect: (key: string) => void;
  disabled?: boolean;
}

/**
 * Compact model picker replacing the local/cloud profile pill (ADR-0121 §3).
 *
 * Each candidate shows its provider placement, context window, and cost so
 * the tradeoff is visible at the point of choice — the whole point of
 * selecting a model by name instead of entering a mode.
 */
export function ModelPicker({
  candidates,
  selectedKey,
  hydrated,
  onSelect,
  disabled = false,
}: ModelPickerProps) {
  const [isOpen, setIsOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!isOpen) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setIsOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [isOpen]);

  const selected = candidates.find((c) => c.key === selectedKey);
  const label = !hydrated ? 'Model' : (selected?.key ?? selectedKey ?? 'Model');

  return (
    <div className="relative" ref={containerRef}>
      <button
        type="button"
        onClick={() => setIsOpen((v) => !v)}
        disabled={disabled}
        aria-label="Choose model"
        aria-expanded={isOpen}
        className="flex-shrink-0 flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg bg-surface border border-line text-ink-muted hover:border-ink-muted hover:text-ink transition-colors whitespace-nowrap disabled:opacity-50 disabled:cursor-not-allowed max-w-[9rem]"
        title={selected?.summary ?? 'Choose model'}
      >
        <span
          className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
            selected?.placement === 'local' ? 'bg-emerald-400' : 'bg-amber-400'
          }`}
        />
        <span className="truncate">{label}</span>
      </button>

      {isOpen && (
        <div
          role="listbox"
          className="absolute bottom-full mb-2 left-0 z-40 w-72 max-h-80 overflow-y-auto rounded-xl border border-line bg-surface shadow-xl py-1"
        >
          {candidates.length === 0 ? (
            <p className="px-3 py-2 text-xs text-ink-muted">No models available</p>
          ) : (
            candidates.map((c) => (
              <button
                key={c.key}
                type="button"
                role="option"
                aria-selected={c.key === selectedKey}
                onClick={() => {
                  onSelect(c.key);
                  setIsOpen(false);
                }}
                className={`flex flex-col items-start gap-0.5 w-full px-3 py-2 text-left text-xs transition-colors ${
                  c.key === selectedKey ? 'ring-1 ring-inset ring-accent/40' : 'hover:bg-line/40'
                }`}
              >
                <span className="flex items-center gap-1.5 w-full">
                  <span
                    className={`w-1.5 h-1.5 rounded-full flex-shrink-0 ${
                      c.placement === 'local' ? 'bg-emerald-400' : 'bg-amber-400'
                    }`}
                  />
                  <span className="font-medium text-ink">{c.key}</span>
                  {c.key === selectedKey && (
                    // dark:text-accent-hover (not accent) — plain accent measured
                    // 3.87:1 against bg-surface dark, just under WCAG AA; the
                    // lighter hover shade clears it (FRE-1265 e2e contrast check).
                    <span className="ml-auto text-accent dark:text-accent-hover text-[10px] font-medium">Selected</span>
                  )}
                </span>
                <span className="text-ink-muted pl-3">
                  {c.placement} · {Math.round(c.context_length / 1000)}K context
                  {c.input_cost_per_token != null
                    ? ` · $${(c.input_cost_per_token * 1_000_000).toFixed(2)}/M in`
                    : ''}
                </span>
                {c.summary && <span className="text-ink-muted pl-3 line-clamp-1">{c.summary}</span>}
              </button>
            ))
          )}
        </div>
      )}
    </div>
  );
}

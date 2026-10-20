'use client';

import { useState, useRef, useEffect } from 'react';

import { EFFORT_LEVELS, EFFORT_NAMES, effortModeLabel } from '@/lib/effort';
import type { EffortLevelModes, SeshatEffort } from '@/lib/types';

export interface EffortSelectorProps {
  /** The level to show; null until the server has answered for this session. */
  level: SeshatEffort | null;
  /** The mode each level requests (ADR-0157 D5); undefined until the config loads. */
  levelModes: EffortLevelModes | undefined;
  /** The modes the session's model declares; undefined while the model is not known. */
  declaredModes: readonly string[] | undefined;
  /** The message of the last failed write, or null. */
  error: string | null;
  onSelect: (level: SeshatEffort) => void;
}

/**
 * Seshat effort selector, next to the model picker (ADR-0157 D2).
 *
 * Each level shows the catalog mode that the session's model runs for it, or "model default"
 * when the model declares no such mode (D5). The level itself is owned by the server; this
 * component only shows it and reports a choice.
 */
export function EffortSelector({
  level,
  levelModes,
  declaredModes,
  error,
  onSelect,
}: EffortSelectorProps) {
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

  return (
    <div className="relative" ref={containerRef}>
      <button
        type="button"
        onClick={() => setIsOpen((v) => !v)}
        aria-label="Choose Seshat effort"
        aria-expanded={isOpen}
        className="flex-shrink-0 flex items-center gap-1.5 text-xs px-2.5 py-1.5 rounded-lg bg-surface border border-line text-ink-muted hover:border-ink-muted hover:text-ink transition-colors whitespace-nowrap"
        title="Seshat effort"
      >
        <span>{level === null ? 'Effort' : EFFORT_NAMES[level]}</span>
      </button>

      {error !== null && (
        <p
          role="alert"
          className="absolute bottom-full mb-2 left-0 z-40 w-60 rounded-lg border border-line bg-surface px-2.5 py-1.5 text-xs text-red-700 dark:text-red-400 shadow-xl"
        >
          {error}
        </p>
      )}

      {isOpen && (
        <div
          role="listbox"
          className="absolute bottom-full mb-2 left-0 z-50 w-56 rounded-xl border border-line bg-surface shadow-xl py-1"
        >
          {EFFORT_LEVELS.map((name) => {
            const modeLabel = effortModeLabel(name, levelModes, declaredModes);
            return (
              <button
                key={name}
                type="button"
                role="option"
                aria-selected={name === level}
                onClick={() => {
                  onSelect(name);
                  setIsOpen(false);
                }}
                className={`flex flex-col items-start gap-0.5 w-full px-3 py-2 text-left text-xs transition-colors ${
                  name === level ? 'ring-1 ring-inset ring-accent/40' : 'hover:bg-line/40'
                }`}
              >
                <span className="flex items-center gap-1.5 w-full">
                  <span className="font-medium text-ink">{EFFORT_NAMES[name]}</span>
                  {name === level && (
                    <span className="ml-auto text-accent dark:text-accent-hover text-[10px] font-medium">
                      Selected
                    </span>
                  )}
                </span>
                {modeLabel !== null && <span className="text-ink-muted">{modeLabel}</span>}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

'use client';

import type { ExecutionProfile } from '@/lib/types';

interface ProfileSelectorProps {
  selected: ExecutionProfile;
  onSelect: (profile: ExecutionProfile) => void;
  disabled?: boolean;
}

interface ProfileOption {
  id: ExecutionProfile;
  label: string;
  model: string;
  description: string;
  cost: string;
}

const PROFILES: ProfileOption[] = [
  {
    id: 'local',
    label: 'Local',
    model: 'Qwen3.5-35B',
    description: 'Runs on your machine. Private, free, no internet required.',
    cost: 'Free',
  },
  {
    id: 'cloud',
    label: 'Cloud',
    model: 'Claude Sonnet',
    description: 'Faster and more capable. Requires backend cloud credentials.',
    cost: '$0.01–0.05 / msg',
  },
];

/**
 * Profile selector shown at the start of a new conversation.
 *
 * Displays local vs. cloud execution profile options with model name,
 * description, and cost estimate. Selection is persisted to the parent
 * via the onSelect callback.
 */
export function ProfileSelector({
  selected,
  onSelect,
  disabled = false,
}: ProfileSelectorProps) {
  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-slate-400 text-center">
        Choose an execution profile for this conversation
      </p>
      <div className="grid grid-cols-2 gap-3">
        {PROFILES.map((profile) => {
          const isSelected = selected === profile.id;
          return (
            <button
              key={profile.id}
              onClick={() => !disabled && onSelect(profile.id)}
              disabled={disabled}
              className={`
                flex flex-col items-start gap-1.5 p-4 rounded-xl border text-left
                transition-all duration-150 cursor-pointer
                ${
                  isSelected
                    ? 'border-blue-500 bg-blue-900/30 ring-1 ring-blue-500/50'
                    : 'border-slate-600 bg-slate-800/50 hover:border-slate-500 hover:bg-slate-800'
                }
                ${disabled ? 'opacity-60 cursor-not-allowed' : ''}
              `}
            >
              <div className="flex items-center gap-2 w-full">
                <span className="text-sm font-semibold text-slate-100">
                  {profile.label}
                </span>
                {isSelected && (
                  <span className="ml-auto text-xs text-blue-400 font-medium">
                    Selected
                  </span>
                )}
              </div>
              <span className="text-xs font-mono text-slate-400">
                {profile.model}
              </span>
              <p className="text-xs text-slate-500 leading-snug">
                {profile.description}
              </p>
              <span
                className={`text-xs font-medium mt-1 ${
                  profile.id === 'local' ? 'text-emerald-400' : 'text-amber-400'
                }`}
              >
                {profile.cost}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}

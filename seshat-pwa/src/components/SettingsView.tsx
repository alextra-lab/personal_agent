/**
 * SettingsView — read-only menu of every parameter that changes a turn (FRE-1702, stage 1
 * of FRE-1698).
 *
 * Nine groups on `/settings`, and the System group on `/settings/system`. Each row shows
 * the parameter name, its live value and the source that wins. A saved choice shows beside
 * the default that it overrides. The menu edits nothing. The one control is "Clear" on a
 * saved choice, which calls `DELETE /api/v1/preferences/constraint`.
 *
 * MANUAL TEST PLAN
 * ================
 * 1. Open /settings from the session drawer ("Settings"). Expected: nine groups, each row
 *    with a value and a source label. The first group follows the open conversation.
 * 2. Open "System" from the header. Expected: background jobs, infrastructure, secrets.
 *    A secret or infrastructure value reads "set" or "not set", never the value.
 * 3. Save a choice with "Remember this choice" on a decision card, then reload /settings.
 *    Expected: the row shows the saved value, "database row", and the default it overrides.
 * 4. Press Clear on that row. Expected: the row reads "not set" and the button is gone.
 */

'use client';

import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import Link from 'next/link';

import { clearConstraintPreference, getSettingsMenu } from '@/lib/agui-client';
import { LAST_SESSION_KEY, resolveLastSessionId } from '@/lib/session';
import type { SettingRow, SettingsGroup, SettingsMenu, SettingValue } from '@/lib/types';

const SAVED_CHOICE_PREFIX = 'saved_choice.';

function isScalar(value: SettingValue): value is string | number | boolean | null {
  return value === null || typeof value !== 'object';
}

function scalarText(value: string | number | boolean | null): string {
  return value === null ? 'null' : String(value);
}

/** One-line preview of a structured value, for the collapsed summary. */
function preview(value: SettingValue): string {
  if (isScalar(value)) return scalarText(value);
  const text = JSON.stringify(value);
  return text.length > 80 ? `${text.slice(0, 79)}…` : text;
}

function ValueView({ value }: { value: SettingValue }) {
  if (isScalar(value)) {
    return <span className="text-ink break-words">{scalarText(value)}</span>;
  }
  if (Array.isArray(value) && value.every(isScalar)) {
    return <span className="text-ink break-words">{value.map((v) => scalarText(v)).join(', ') || '(empty)'}</span>;
  }
  const entries: [string, SettingValue][] = Array.isArray(value)
    ? value.map((v, i) => [String(i), v])
    : Object.entries(value);
  return (
    <details className="text-ink">
      <summary className="cursor-pointer text-ink-muted break-all">{preview(value)}</summary>
      <dl className="mt-1 ml-2 space-y-0.5 border-l border-line pl-2">
        {entries.map(([key, inner]) => (
          <div key={key} className="flex gap-2">
            <dt className="text-ink-muted flex-shrink-0">{key}</dt>
            <dd className="min-w-0">
              <ValueView value={inner} />
            </dd>
          </div>
        ))}
      </dl>
    </details>
  );
}

function SourceBadge({ source }: { source: string }) {
  return (
    <span className="text-[10px] font-semibold uppercase tracking-wide px-1.5 py-0.5 rounded border border-line text-ink-muted flex-shrink-0">
      {source}
    </span>
  );
}

function Row({ row, onClear }: { row: SettingRow; onClear: (row: SettingRow) => void }) {
  return (
    <div data-testid="setting-row" className="px-3 py-2.5 text-sm space-y-1">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:gap-3">
        <span className="font-medium text-ink sm:w-56 sm:flex-shrink-0 break-all">{row.name}</span>
        <span className="flex-1 min-w-0">
          <ValueView value={row.value} />
        </span>
        <div className="flex items-center gap-2 sm:flex-shrink-0">
          <SourceBadge source={row.source} />
          {row.clearable && (
            <button
              type="button"
              onClick={() => onClear(row)}
              className="text-xs px-2 py-0.5 rounded border border-accent/40 text-accent hover:bg-surface flex-shrink-0"
            >
              Clear
            </button>
          )}
        </div>
      </div>
      {row.overrides && (
        <p className="text-xs text-ink-muted sm:pl-[15rem]">
          overrides <ValueView value={row.overrides.value} /> ({row.overrides.source})
        </p>
      )}
      {row.note && <p className="text-xs text-ink-muted sm:pl-[15rem]">{row.note}</p>}
    </div>
  );
}

function Group({ group, onClear }: { group: SettingsGroup; onClear: (row: SettingRow) => void }) {
  const headingId = `settings-group-${group.id}`;
  return (
    <section aria-labelledby={headingId}>
      <h2
        id={headingId}
        className="text-xs font-semibold uppercase tracking-wide text-ink-muted mb-2"
      >
        {group.title}
      </h2>
      <div className="rounded-xl border border-line divide-y divide-line overflow-hidden">
        {group.error ? (
          <p className="px-3 py-4 text-xs text-ink-muted">This group could not be loaded.</p>
        ) : (
          group.rows.map((row) => <Row key={row.name} row={row} onClear={onClear} />)
        )}
      </div>
    </section>
  );
}

function ListSkeleton() {
  return (
    <div className="px-4 py-6 space-y-2">
      {[0, 1, 2, 3].map((i) => (
        <div key={i} className="h-10 rounded-lg bg-surface/60 animate-pulse" />
      ))}
    </div>
  );
}

export function SettingsView({ view }: { view: 'menu' | 'system' }) {
  const searchParams = useSearchParams();
  const querySessionId = searchParams.get('session') ?? undefined;
  const [lastSessionId, setLastSessionId] = useState<string | undefined>(() =>
    typeof window !== 'undefined' ? (localStorage.getItem(LAST_SESSION_KEY) ?? undefined) : undefined,
  );
  const [menu, setMenu] = useState<SettingsMenu | undefined>(undefined);
  const [loadFailed, setLoadFailed] = useState(false);
  const [clearFailed, setClearFailed] = useState(false);

  useEffect(() => {
    // A query param or a stored key is known synchronously above. Ask the server only
    // when both are absent, as ObserveView does.
    if (querySessionId || lastSessionId) return;
    let cancelled = false;
    resolveLastSessionId().then((id) => {
      if (!cancelled && id) setLastSessionId(id);
    });
    return () => {
      cancelled = true;
    };
  }, [querySessionId, lastSessionId]);

  const sessionId = querySessionId ?? lastSessionId;

  const load = useCallback(() => {
    const apply = (m: SettingsMenu) => {
      setMenu(m);
      setLoadFailed(false);
    };
    return getSettingsMenu(sessionId)
      .then(apply)
      .catch(() => {
        // A stored or linked session can be gone (404) or malformed (422). The menu must
        // not stay stuck on it, so retry once without a session and show the defaults.
        if (!sessionId) {
          setLoadFailed(true);
          return;
        }
        return getSettingsMenu(undefined).then(apply).catch(() => setLoadFailed(true));
      });
  }, [sessionId]);

  useEffect(() => {
    void load();
  }, [load]);

  const handleClear = useCallback(
    (row: SettingRow) => {
      if (!row.name.startsWith(SAVED_CHOICE_PREFIX)) return;
      setClearFailed(false);
      clearConstraintPreference(row.name.slice(SAVED_CHOICE_PREFIX.length))
        .then(() => load())
        .catch(() => setClearFailed(true));
    },
    [load],
  );

  const title = view === 'menu' ? 'Settings' : 'System';

  return (
    <div className="min-h-full bg-bg text-ink flex flex-col">
      <header
        className="flex items-center gap-3 px-4 border-b border-line bg-bg/80 backdrop-blur-sm flex-shrink-0"
        style={{ paddingTop: 'calc(env(safe-area-inset-top, 0px) + 0.75rem)', paddingBottom: '0.75rem' }}
      >
        <Link
          href="/"
          className="flex items-center gap-1 text-sm text-ink-muted hover:text-ink transition-colors"
        >
          ← Conversations
        </Link>
        <span className="text-line" aria-hidden="true">|</span>
        <h1 className="text-sm font-semibold text-ink">{title}</h1>
        <span className="flex-1" />
        {view === 'menu' ? (
          <Link href="/settings/system" className="text-sm text-ink-muted hover:text-ink transition-colors">
            System
          </Link>
        ) : (
          <Link href="/settings" className="text-sm text-ink-muted hover:text-ink transition-colors">
            Settings
          </Link>
        )}
      </header>

      <main className="flex-1 overflow-y-auto px-4 py-4 space-y-6">
        {clearFailed && (
          <p role="alert" className="text-xs text-ink">
            Could not clear the saved choice. Try again.
          </p>
        )}
        {loadFailed && (
          <p role="alert" className="text-xs text-ink">
            Could not load the settings. Reload the page to try again.
          </p>
        )}
        {!menu && !loadFailed && <ListSkeleton />}
        {menu && view === 'menu' && (
          <>
            <p className="text-xs text-ink-muted">
              This menu shows the live values. It edits nothing, except the Clear button on a saved
              choice.
            </p>
            {menu.groups.map((group) => (
              <Group key={group.id} group={group} onClear={handleClear} />
            ))}
          </>
        )}
        {menu && view === 'system' && <Group group={menu.system} onClear={handleClear} />}
      </main>
    </div>
  );
}

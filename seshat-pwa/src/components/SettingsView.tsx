/**
 * SettingsView — menu of every parameter that changes a turn (FRE-1702, FRE-1722, FRE-1723, FRE-1724; FRE-1698).
 *
 * Nine groups on `/settings`, and the System group on `/settings/system`. Each group is an
 * accordion section, closed by default (FRE-1710). The closed header shows the group name,
 * its parameter count, and a marker when the group holds a saved choice. Each row shows
 * the parameter name, its live value and the source that wins. A saved choice shows beside
 * the default that it overrides. An editable row carries an edit control, a default line, and Clear
 * when an edit exists. A read-only row shows its reason. A saved choice, a conversation row and the
 * location consent write through their own routes. The others write to `/api/v1/settings/overrides`,
 * for the owner only.
 *
 * MANUAL TEST PLAN
 * ================
 * 1. Open /settings from the session drawer ("Settings"). Expected: nine closed groups, each
 *    header with a name and a count. Tap one header. Expected: only that group opens, each row
 *    with a value and a source label. Tap it again. Expected: it closes. The first group
 *    follows the open conversation.
 * 2. Open "System" from the header, then tap its header. Expected: background jobs,
 *    infrastructure, secrets. A secret or infrastructure value reads "set" or "not set",
 *    never the value.
 * 3. Save a choice with "Remember this choice" on a decision card, then reload /settings.
 *    Expected: the "Saved choices" header shows a saved-choice marker while closed. Open it.
 *    Expected: the row shows the saved value, "database row", and the default it overrides.
 * 4. Press Clear on that row. Expected: the row reads "not set" and the button is gone.
 * 5. As the owner, open "Models per role". Expected: each row has a select, a disabled Save, and
 *    a file default line. Choose another model and press Save. Expected: the row shows the new
 *    value, "override", and Clear. Press Clear. Expected: the file value and "config file".
 * 6. As another user, open "Models per role". Expected: no select and no Clear.
 * 7. As the owner, open "Budgets and cost". Change a cap and press Save, then reload. Expected: the
 *    new value, "override", the file default, and Clear. Enter -1 and press Save. Expected: the row
 *    shows the reason, and the value stays.
 * 8. As the owner, open "Effort presets and model modes". Change the rounds of a preset and press
 *    Save, then reload. Expected: the new value, "override", the file default, and Clear.
 * 9. As the owner, open "How a turn runs". Change a number setting and press Save. Expected: the new
 *    value, "override", the default, and Clear. Press Clear. Expected: the first value.
 * 10. Open "System". Find a row that says "applies at the next gateway restart". Change it and press
 *    Save. Expected: the old value, and a note that the stored value waits for the restart.
 * 11. Open a secret row in "System". Expected: "set" or "not set", the reason, and no control.
 * 12. Open "Tools and governance". Edit the JSON of a tool. Enter invalid JSON. Expected: Save is off.
 */

'use client';

import { useCallback, useEffect, useState } from 'react';
import { useSearchParams } from 'next/navigation';
import Link from 'next/link';

import {
  clearConstraintPreference,
  clearSettingOverride,
  getSettingsMenu,
  setConstraintPreference,
  setSessionEffort,
  setSessionSelection,
  setSettingOverride,
  updateLocationPreference,
} from '@/lib/agui-client';
import { LAST_SESSION_KEY, resolveLastSessionId } from '@/lib/session';
import type { SeshatEffort, SettingRow, SettingsGroup, SettingsMenu, SettingValue } from '@/lib/types';

const SAVED_CHOICE_PREFIX = 'saved_choice.';

/** Writes one edit and reloads the menu. Rejects with the API's reason. */
type SaveHandler = (row: SettingRow, value: SettingValue) => Promise<unknown>;

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

const FIELD_CLASS = 'min-w-0 text-xs px-2 py-1 rounded border border-line bg-bg text-ink disabled:opacity-40';

/**
 * Save and its error text. A refused save keeps the draft and shows the reason here, because a
 * banner can be off-screen on a phone.
 */
function useSaveWithReason(row: SettingRow, onSave: SaveHandler) {
  const [error, setError] = useState<string | undefined>(undefined);
  const save = (value: SettingValue) => {
    setError(undefined);
    onSave(row, value).catch((err: unknown) =>
      setError(`Could not save: ${err instanceof Error ? err.message : 'unknown error'}`),
    );
  };
  return { error, save };
}

function SaveButton({ disabled, onClick }: { disabled: boolean; onClick: () => void }) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className="text-xs px-2 py-0.5 rounded border border-accent/40 text-accent hover:bg-surface flex-shrink-0 disabled:opacity-40"
    >
      Save
    </button>
  );
}

function SaveError({ error }: { error: string | undefined }) {
  if (!error) return null;
  return (
    <p role="alert" className="text-xs text-ink">
      {error}
    </p>
  );
}

/** A select and a Save button. The draft follows the stored value when the menu reloads. */
function EditControl({ row, onSave }: { row: SettingRow; onSave: SaveHandler }) {
  const current = row.edit?.current ?? '';
  const [draft, setDraft] = useState(current);
  const { error, save } = useSaveWithReason(row, onSave);
  useEffect(() => setDraft(current), [current]);

  return (
    <div className="space-y-1 sm:pl-[15rem]">
      <div className="flex items-center gap-2">
        <select
          aria-label={`New value for ${row.name}`}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          className={FIELD_CLASS}
        >
          {current === '' && (
            <option value="" disabled>
              Choose a value
            </option>
          )}
          {row.edit?.options.map((option) => (
            <option key={option} value={option}>
              {option}
            </option>
          ))}
        </select>
        <SaveButton disabled={draft === '' || draft === current} onClick={() => save(draft)} />
      </div>
      <SaveError error={error} />
    </div>
  );
}

/** A number input and a Save button, for a budget cap in US dollars. */
function CapControl({ row, onSave }: { row: SettingRow; onSave: SaveHandler }) {
  const current = row.edit?.current ?? '';
  const [draft, setDraft] = useState(current);
  const { error, save } = useSaveWithReason(row, onSave);
  useEffect(() => setDraft(current), [current]);
  const unusable = draft === '' || Number.isNaN(Number(draft)) || Number(draft) === Number(current);

  return (
    <div className="space-y-1 sm:pl-[15rem]">
      <div className="flex items-center gap-2">
        <input
          type="number"
          step="any"
          aria-label={`New value for ${row.name}, in US dollars`}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          className={FIELD_CLASS}
        />
        <SaveButton disabled={unusable} onClick={() => save(Number(draft))} />
      </div>
      <SaveError error={error} />
    </div>
  );
}

interface PresetDraft {
  expansion: boolean;
  rounds: string;
  policy: string;
  mode: string;
}

function presetDraft(value: SettingValue): PresetDraft {
  const preset = (value ?? {}) as { [key: string]: SettingValue };
  return {
    expansion: preset.expansion === true,
    rounds: preset.sub_agent_rounds == null ? '' : String(preset.sub_agent_rounds),
    policy: preset.planner_policy == null ? '' : String(preset.planner_policy),
    mode: preset.primary_mode == null ? '' : String(preset.primary_mode),
  };
}

/** The value that a draft saves. With expansion off, the rounds and the policy have no use. */
function presetValue(draft: PresetDraft): SettingValue {
  return {
    expansion: draft.expansion,
    sub_agent_rounds: draft.expansion && draft.rounds !== '' ? Number(draft.rounds) : null,
    planner_policy: draft.expansion && draft.policy !== '' ? draft.policy : null,
    primary_mode: draft.mode !== '' ? draft.mode : null,
  };
}

/** The fields of an effort preset and a Save button. Save sends the whole preset. */
function PresetControl({ row, onSave }: { row: SettingRow; onSave: SaveHandler }) {
  const liveKey = JSON.stringify(row.value);
  const [draft, setDraft] = useState(() => presetDraft(row.value));
  const { error, save } = useSaveWithReason(row, onSave);
  useEffect(() => setDraft(presetDraft(JSON.parse(liveKey) as SettingValue)), [liveKey]);

  const update = (change: Partial<PresetDraft>) => setDraft((old) => ({ ...old, ...change }));
  const value = presetValue(draft);
  const unchanged = JSON.stringify(value) === JSON.stringify(presetValue(presetDraft(row.value)));

  return (
    <div className="space-y-1 sm:pl-[15rem]">
      <div className="flex flex-wrap items-center gap-2 text-xs text-ink-muted">
        <label className="flex items-center gap-1">
          <input
            type="checkbox"
            checked={draft.expansion}
            onChange={(e) => update({ expansion: e.target.checked })}
          />
          expansion
        </label>
        <label className="flex items-center gap-1">
          sub_agent_rounds
          <input
            type="number"
            min={1}
            step={1}
            disabled={!draft.expansion}
            value={draft.rounds}
            onChange={(e) => update({ rounds: e.target.value })}
            className={`${FIELD_CLASS} w-16`}
          />
        </label>
        <label className="flex items-center gap-1">
          planner_policy
          <select
            disabled={!draft.expansion}
            value={draft.policy}
            onChange={(e) => update({ policy: e.target.value })}
            className={FIELD_CLASS}
          >
            <option value="">none</option>
            {row.edit?.preset_choices?.planner_policy?.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1">
          primary_mode
          <select value={draft.mode} onChange={(e) => update({ mode: e.target.value })} className={FIELD_CLASS}>
            <option value="">model default</option>
            {row.edit?.preset_choices?.primary_mode?.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>
        <SaveButton disabled={unchanged} onClick={() => save(value)} />
      </div>
      <SaveError error={error} />
    </div>
  );
}

/** The value that a draft saves, or `undefined` when the draft is not valid. */
function settingValue(row: SettingRow, draft: string): SettingValue | undefined {
  const nullable = row.edit?.nullable === true;
  if (draft === '') return nullable ? null : row.edit?.input === 'text' ? '' : undefined;
  switch (row.edit?.input) {
    case 'boolean':
      return draft === 'true';
    case 'number':
      return Number.isNaN(Number(draft)) ? undefined : Number(draft);
    case 'json':
      try {
        return JSON.parse(draft) as SettingValue;
      } catch {
        return undefined;
      }
    default:
      return draft;
  }
}

/** A control for one setting, by the type that the server states. A hidden value starts empty. */
function SettingControl({ row, onSave }: { row: SettingRow; onSave: SaveHandler }) {
  const edit = row.edit;
  const current = edit?.current ?? '';
  const [draft, setDraft] = useState(current);
  const { error, save } = useSaveWithReason(row, onSave);
  useEffect(() => setDraft(current), [current]);

  const label = `New value for ${row.name}`;
  const value = settingValue(row, draft);
  const hidden = edit?.current == null && row.value === 'set';
  const unusable = value === undefined || draft === current;
  const options = edit?.input === 'boolean' ? ['true', 'false'] : (edit?.options ?? []);

  let field;
  if (edit?.input === 'boolean' || edit?.input === 'select') {
    field = (
      <select aria-label={label} value={draft} onChange={(e) => setDraft(e.target.value)} className={FIELD_CLASS}>
        {(current === '' || edit?.nullable) && <option value="">{edit?.nullable ? 'none' : 'Choose a value'}</option>}
        {options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    );
  } else if (edit?.input === 'json') {
    field = (
      <textarea
        aria-label={label}
        rows={4}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        className={`${FIELD_CLASS} w-full font-mono`}
      />
    );
  } else {
    field = (
      <input
        type={edit?.input === 'number' ? 'number' : 'text'}
        step="any"
        aria-label={label}
        placeholder={hidden ? 'The value is hidden. Enter a new value.' : undefined}
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        className={`${FIELD_CLASS} flex-1`}
      />
    );
  }

  return (
    <div className="space-y-1 sm:pl-[15rem]">
      <div className="flex items-center gap-2">
        {field}
        <SaveButton disabled={unusable} onClick={() => save(value as SettingValue)} />
      </div>
      <SaveError error={error} />
    </div>
  );
}

function RowControl({ row, onSave }: { row: SettingRow; onSave: SaveHandler }) {
  switch (row.edit?.kind) {
    case undefined:
      return null;
    case 'budget_cap':
      return <CapControl row={row} onSave={onSave} />;
    case 'effort_preset':
      return <PresetControl row={row} onSave={onSave} />;
    case 'setting':
    case 'location_consent':
      return <SettingControl row={row} onSave={onSave} />;
    default:
      return <EditControl row={row} onSave={onSave} />;
  }
}

function Row({
  row,
  onClear,
  onSave,
}: {
  row: SettingRow;
  onClear: (row: SettingRow) => void;
  onSave: SaveHandler;
}) {
  const repeatsDefault = JSON.stringify(row.overrides) === JSON.stringify(row.file_default);
  return (
    <div data-testid="setting-row" className="px-3 py-2.5 text-sm space-y-1">
      <div className="flex flex-col gap-1 sm:flex-row sm:items-start sm:gap-3">
        <span className="font-medium text-ink sm:w-56 sm:flex-shrink-0 break-all">{row.name}</span>
        <span data-testid="setting-live-value" className="flex-1 min-w-0">
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
      {row.overrides && !repeatsDefault && (
        <p className="text-xs text-ink-muted sm:pl-[15rem]">
          overrides <ValueView value={row.overrides.value} /> ({row.overrides.source})
        </p>
      )}
      {row.file_default && (
        <p data-testid="setting-file-default" className="text-xs text-ink-muted sm:pl-[15rem]">
          {row.file_default.source === 'config file' ? 'file default' : 'default'}{' '}
          <ValueView value={row.file_default.value} /> ({row.file_default.source})
        </p>
      )}
      {row.note && <p className="text-xs text-ink-muted sm:pl-[15rem]">{row.note}</p>}
      {row.read_only_reason && (
        <p data-testid="setting-read-only-reason" className="text-xs text-ink-muted sm:pl-[15rem]">
          Read-only: {row.read_only_reason}
        </p>
      )}
      <RowControl row={row} onSave={onSave} />
    </div>
  );
}

function ChevronIcon({ expanded }: { expanded: boolean }) {
  return (
    <svg
      aria-hidden="true"
      className={`h-3 w-3 flex-shrink-0 transition-transform ${expanded ? 'rotate-90' : ''}`}
      xmlns="http://www.w3.org/2000/svg"
      viewBox="0 0 20 20"
      fill="currentColor"
    >
      <path
        fillRule="evenodd"
        d="M7.21 14.77a.75.75 0 01.02-1.06L11.168 10 7.23 6.29a.75.75 0 111.04-1.08l4.5 4.25a.75.75 0 010 1.08l-4.5 4.25a.75.75 0 01-1.06-.02z"
        clipRule="evenodd"
      />
    </svg>
  );
}

/** Accordion section: a header button with the name and count, rows only while open. */
function Group({
  group,
  onClear,
  onSave,
}: {
  group: SettingsGroup;
  onClear: (row: SettingRow) => void;
  onSave: SaveHandler;
}) {
  const [open, setOpen] = useState(false);
  const headingId = `settings-group-${group.id}`;
  const clearable = group.rows.filter((row) => row.clearable);
  const savedChoices = clearable.filter((row) => row.name.startsWith(SAVED_CHOICE_PREFIX)).length;
  const overrides = clearable.length - savedChoices;
  return (
    <section aria-labelledby={headingId}>
      <h2 id={headingId} className="mb-2">
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="flex w-full items-center gap-2 text-left text-xs font-semibold uppercase tracking-wide text-ink-muted hover:text-ink transition-colors"
        >
          <ChevronIcon expanded={open} />
          <span>
            {group.title}
            {!group.error && ` (${group.rows.length})`}
          </span>
          {group.error && <SourceBadge source="unavailable" />}
          {savedChoices > 0 && (
            <SourceBadge source={savedChoices === 1 ? 'saved choice' : `${savedChoices} saved choices`} />
          )}
          {overrides > 0 && <SourceBadge source={overrides === 1 ? 'override' : `${overrides} overrides`} />}
        </button>
      </h2>
      {open && (
        <div className="rounded-xl border border-line divide-y divide-line overflow-hidden">
          {group.error ? (
            <p className="px-3 py-4 text-xs text-ink-muted">This group could not be loaded.</p>
          ) : (
            group.rows.map((row) => <Row key={row.name} row={row} onClear={onClear} onSave={onSave} />)
          )}
        </div>
      )}
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
  const [writeError, setWriteError] = useState<string | undefined>(undefined);

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
      setWriteError(undefined);
      const write: Promise<unknown> = row.name.startsWith(SAVED_CHOICE_PREFIX)
        ? clearConstraintPreference(row.name.slice(SAVED_CHOICE_PREFIX.length))
        : clearSettingOverride(row.name);
      write.then(() => load()).catch(() => setWriteError(`Could not clear ${row.name}. Try again.`));
    },
    [load],
  );

  const handleSave = useCallback(
    (row: SettingRow, value: SettingValue) => {
      const writeFor = (): Promise<unknown> => {
        switch (row.edit?.kind) {
          case 'saved_choice':
            return setConstraintPreference(row.name.slice(SAVED_CHOICE_PREFIX.length), String(value));
          case 'session_primary':
            return setSessionSelection(sessionId ?? '', 'primary', String(value));
          case 'session_effort':
            return setSessionEffort(sessionId ?? '', value as SeshatEffort);
          case 'location_consent':
            return updateLocationPreference(value === true);
          default:
            return setSettingOverride(row.name, value);
        }
      };
      return writeFor().then(() => load());
    },
    [load, sessionId],
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
        {writeError && (
          <p role="alert" className="text-xs text-ink">
            {writeError}
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
              This menu shows the live values. A row with a Save button can be edited. An edit applies
              with no rebuild, at the next turn or at the next gateway restart, as the row states. Clear
              restores the default.
            </p>
            {menu.groups.map((group) => (
              <Group key={group.id} group={group} onClear={handleClear} onSave={handleSave} />
            ))}
          </>
        )}
        {menu && view === 'system' && <Group group={menu.system} onClear={handleClear} onSave={handleSave} />}
      </main>
    </div>
  );
}

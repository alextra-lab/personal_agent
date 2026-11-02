/**
 * Tests for SettingsView (FRE-1702, FRE-1722; FRE-1698).
 *
 * AC-1 (unit half): the nine groups render, and the System view renders the System group;
 *   every row shows its name, live value and winning source.
 * AC-2: a saved choice shows beside the default that it overrides.
 * AC-5: a row with no `edit` field has no control, except "Clear" on a clearable row.
 *
 * FRE-1710: each group is an accordion section, closed by default.
 * AC-1: every group is closed at first render, a click opens only that group, a second click closes it.
 * AC-2: a header shows the group name and its parameter count, and a marker for a saved choice.
 * AC-3: a header is a focusable button with `aria-expanded` false when closed and true when open.
 *
 * FRE-1722 AC-4: a role binding and a saved choice each show the edit control, the live value, the
 *   file default and Clear.
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi, describe, it, expect, beforeEach } from 'vitest';

import type { SettingsMenu } from '@/lib/types';

const mockGetSettingsMenu = vi.fn();
const mockClear = vi.fn();
const mockSetPreference = vi.fn();
const mockSetOverride = vi.fn();
const mockClearOverride = vi.fn();
const mockResolveLastSessionId = vi.fn();
let mockSearchParams = new URLSearchParams();

vi.mock('@/lib/agui-client', () => ({
  getSettingsMenu: (...args: unknown[]) => mockGetSettingsMenu(...args),
  clearConstraintPreference: (...args: unknown[]) => mockClear(...args),
  setConstraintPreference: (...args: unknown[]) => mockSetPreference(...args),
  setSettingOverride: (...args: unknown[]) => mockSetOverride(...args),
  clearSettingOverride: (...args: unknown[]) => mockClearOverride(...args),
}));

vi.mock('@/lib/session', () => ({
  LAST_SESSION_KEY: 'seshat_last_session_id',
  resolveLastSessionId: (...args: unknown[]) => mockResolveLastSessionId(...args),
}));

vi.mock('next/navigation', () => ({
  useSearchParams: () => mockSearchParams,
}));

import { SettingsView } from '@/components/SettingsView';

const GROUP_TITLES = [
  'This conversation',
  'Saved choices',
  'Models per role',
  'Effort presets and model modes',
  'Budgets and cost',
  'How a turn runs',
  'Memory and recall',
  'Tools and governance',
  'Grounding and quality',
];

function menu(overrides: Partial<SettingsMenu> = {}): SettingsMenu {
  const ids = [
    'conversation',
    'saved_choices',
    'models_per_role',
    'effort_and_modes',
    'budgets',
    'turn_runs',
    'memory',
    'tools',
    'grounding',
  ];
  return {
    groups: ids.map((id, i) => ({
      id,
      title: GROUP_TITLES[i],
      error: null,
      rows: [
        {
          name: `${id}.example`,
          value: `value-of-${id}`,
          source: 'config file',
          overrides: null,
          clearable: false,
          note: null,
          file_default: null,
          edit: null,
        },
      ],
    })),
    system: {
      id: 'system',
      title: 'System (read-only)',
      error: null,
      rows: [
        {
          name: 'setting.neo4j_password',
          value: 'set',
          source: '.env',
          overrides: null,
          clearable: false,
          note: null,
          file_default: null,
          edit: null,
        },
      ],
    },
    ...overrides,
  };
}

function withSavedChoice(): SettingsMenu {
  const m = menu();
  m.groups[1] = {
    ...m.groups[1],
    rows: [
      {
        name: 'saved_choice.artifact_builder',
        value: 'claude_sonnet',
        source: 'database row',
        overrides: { value: 'gpt-6.1-sol', source: 'config file' },
        clearable: true,
        note: null,
        file_default: null,
        edit: null,
      },
      {
        name: 'saved_choice.tool_iteration_limit',
        value: 'not set',
        source: 'code default',
        overrides: null,
        clearable: false,
        note: null,
        file_default: null,
        edit: null,
      },
    ],
  };
  return m;
}

/** The accordion header of a group: the button inside the group heading. */
function header(title: string): HTMLElement {
  return within(screen.getByRole('heading', { level: 2, name: new RegExp(`^${title}`) })).getByRole('button');
}

async function openGroup(title: string): Promise<void> {
  await userEvent.click(header(title));
}

beforeEach(() => {
  mockGetSettingsMenu.mockReset();
  mockClear.mockReset();
  mockSetPreference.mockReset();
  mockSetOverride.mockReset();
  mockClearOverride.mockReset();
  mockResolveLastSessionId.mockReset();
  mockResolveLastSessionId.mockResolvedValue(undefined);
  mockSearchParams = new URLSearchParams();
  localStorage.clear();
});

describe('SettingsView menu page (AC-1)', () => {
  it('shows the nine groups, each row with its value and source, and no System group', async () => {
    mockGetSettingsMenu.mockResolvedValue(menu());
    render(<SettingsView view="menu" />);

    for (const title of GROUP_TITLES) {
      expect(await screen.findByRole('heading', { name: new RegExp(`^${title}`) })).toBeInTheDocument();
    }
    expect(screen.queryByRole('heading', { name: /System/ })).not.toBeInTheDocument();

    await openGroup('How a turn runs');
    const row = screen.getByText('turn_runs.example').closest('[data-testid="setting-row"]') as HTMLElement;
    expect(within(row).getByText('value-of-turn_runs')).toBeInTheDocument();
    expect(within(row).getByText('config file')).toBeInTheDocument();
  });

  it('links to the System page', async () => {
    mockGetSettingsMenu.mockResolvedValue(menu());
    render(<SettingsView view="menu" />);
    const link = await screen.findByRole('link', { name: /System/ });
    expect(link).toHaveAttribute('href', '/settings/system');
  });
});

describe('SettingsView System page (AC-1)', () => {
  it('shows only the System group, with a hidden value as set', async () => {
    mockGetSettingsMenu.mockResolvedValue(menu());
    render(<SettingsView view="system" />);

    expect(await screen.findByRole('heading', { level: 2, name: /^System \(read-only\)/ })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /^Saved choices/ })).not.toBeInTheDocument();
    await openGroup('System');
    const row = screen.getByText('setting.neo4j_password').closest('[data-testid="setting-row"]') as HTMLElement;
    expect(within(row).getByText('set')).toBeInTheDocument();
    expect(within(row).getByText('.env')).toBeInTheDocument();
  });
});

describe('saved choice beside its default (AC-2)', () => {
  it('shows the overridden default and its source next to the saved value', async () => {
    mockGetSettingsMenu.mockResolvedValue(withSavedChoice());
    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^Saved choices/ });
    await openGroup('Saved choices');

    const row = (await screen.findByText('saved_choice.artifact_builder')).closest(
      '[data-testid="setting-row"]',
    ) as HTMLElement;
    expect(within(row).getByText('claude_sonnet')).toBeInTheDocument();
    expect(within(row).getByText('database row')).toBeInTheDocument();
    expect(within(row).getByText(/overrides/i)).toBeInTheDocument();
    expect(within(row).getByText('gpt-6.1-sol')).toBeInTheDocument();
  });
});

describe('a row with no edit field has no control (AC-5)', () => {
  it('has no input, select, textarea, switch or checkbox', async () => {
    mockGetSettingsMenu.mockResolvedValue(withSavedChoice());
    const { container } = render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^Saved choices/ });
    await openGroup('Saved choices');
    await screen.findByText('saved_choice.artifact_builder');

    expect(container.querySelectorAll('input, select, textarea')).toHaveLength(0);
    expect(screen.queryAllByRole('switch')).toHaveLength(0);
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
    expect(screen.queryAllByRole('combobox')).toHaveLength(0);
  });

  it('has one Clear button per clearable row, and each says Clear', async () => {
    mockGetSettingsMenu.mockResolvedValue(withSavedChoice());
    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^Saved choices/ });
    await openGroup('Saved choices');
    await screen.findByText('saved_choice.artifact_builder');

    const buttons = screen.getAllByRole('button', { name: 'Clear' });
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveTextContent(/^Clear$/);
  });

  it('shows no Clear button when no saved choice exists', async () => {
    mockGetSettingsMenu.mockResolvedValue(menu());
    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^This conversation/ });
    await openGroup('This conversation');
    expect(screen.getByText('conversation.example')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Clear' })).not.toBeInTheDocument();
  });
});

describe('Clear', () => {
  it('calls the DELETE with the constraint name, then reloads the menu', async () => {
    mockGetSettingsMenu.mockResolvedValueOnce(withSavedChoice());
    mockClear.mockResolvedValue({ constraint_name: 'artifact_builder', cleared: true });
    const cleared = withSavedChoice();
    cleared.groups[1].rows[0] = {
      ...cleared.groups[1].rows[0],
      value: 'not set',
      source: 'code default',
      overrides: null,
      clearable: false,
    };
    mockGetSettingsMenu.mockResolvedValueOnce(cleared);

    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^Saved choices/ });
    await openGroup('Saved choices');
    await userEvent.click(await screen.findByRole('button', { name: 'Clear' }));

    expect(mockClear).toHaveBeenCalledWith('artifact_builder');
    await waitFor(() => expect(mockGetSettingsMenu).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole('button', { name: 'Clear' })).not.toBeInTheDocument());
  });

  it('shows an error and keeps the row when the DELETE fails', async () => {
    mockGetSettingsMenu.mockResolvedValue(withSavedChoice());
    mockClear.mockRejectedValue(new Error('clearConstraintPreference failed: 500'));

    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^Saved choices/ });
    await openGroup('Saved choices');
    await userEvent.click(await screen.findByRole('button', { name: 'Clear' }));

    expect(await screen.findByRole('alert')).toHaveTextContent(/could not clear/i);
    expect(screen.getByRole('button', { name: 'Clear' })).toBeInTheDocument();
  });
});

describe('session wiring', () => {
  it('passes the ?session= id to the API', async () => {
    mockSearchParams = new URLSearchParams('session=11111111-1111-1111-1111-111111111111');
    mockGetSettingsMenu.mockResolvedValue(menu());
    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^This conversation/ });
    expect(mockGetSettingsMenu).toHaveBeenCalledWith('11111111-1111-1111-1111-111111111111');
  });

  it('falls back to the stored last session', async () => {
    localStorage.setItem('seshat_last_session_id', '22222222-2222-2222-2222-222222222222');
    mockGetSettingsMenu.mockResolvedValue(menu());
    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^This conversation/ });
    expect(mockGetSettingsMenu).toHaveBeenCalledWith('22222222-2222-2222-2222-222222222222');
  });

  it('asks the server for the last session when the query and storage are empty', async () => {
    mockResolveLastSessionId.mockResolvedValue('33333333-3333-3333-3333-333333333333');
    mockGetSettingsMenu.mockResolvedValue(menu());
    render(<SettingsView view="menu" />);
    await waitFor(() =>
      expect(mockGetSettingsMenu).toHaveBeenCalledWith('33333333-3333-3333-3333-333333333333'),
    );
  });
});

describe('a stale session id', () => {
  it('retries without the id and shows the defaults, so the page is never stuck', async () => {
    mockSearchParams = new URLSearchParams('session=44444444-4444-4444-4444-444444444444');
    mockGetSettingsMenu
      .mockRejectedValueOnce(new Error('getSettingsMenu failed: 404'))
      .mockResolvedValueOnce(menu());
    render(<SettingsView view="menu" />);

    expect(await screen.findByRole('heading', { name: /^This conversation/ })).toBeInTheDocument();
    expect(mockGetSettingsMenu).toHaveBeenNthCalledWith(1, '44444444-4444-4444-4444-444444444444');
    expect(mockGetSettingsMenu).toHaveBeenNthCalledWith(2, undefined);
    expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  });

  it('shows the load error when the retry without an id fails too', async () => {
    mockSearchParams = new URLSearchParams('session=44444444-4444-4444-4444-444444444444');
    mockGetSettingsMenu.mockRejectedValue(new Error('getSettingsMenu failed: 500'));
    render(<SettingsView view="menu" />);
    expect(await screen.findByRole('alert')).toHaveTextContent(/could not load/i);
  });
});

describe('failure states', () => {
  it('says a group is unavailable, and still renders the other groups', async () => {
    const m = menu();
    m.groups[4] = { ...m.groups[4], rows: [], error: 'unavailable' };
    mockGetSettingsMenu.mockResolvedValue(m);
    render(<SettingsView view="menu" />);

    await screen.findByRole('heading', { name: /^Budgets and cost/ });
    await openGroup('Budgets and cost');
    expect(await screen.findByText(/could not be loaded/i)).toBeInTheDocument();
    await openGroup('Memory and recall');
    expect(screen.getByText('memory.example')).toBeInTheDocument();
  });

  it('shows an error when the menu cannot be fetched', async () => {
    mockGetSettingsMenu.mockRejectedValue(new Error('getSettingsMenu failed: 500'));
    render(<SettingsView view="menu" />);
    expect(await screen.findByRole('alert')).toHaveTextContent(/could not load/i);
  });
});

describe('structured values', () => {
  it('renders a dict value with its keys and values', async () => {
    const m = menu();
    m.groups[3].rows = [
      {
        name: 'effort.level.deep',
        value: { expansion: true, sub_agent_rounds: 20, planner_policy: 'baseline' },
        source: 'config file',
        overrides: null,
        clearable: false,
        note: null,
        file_default: null,
        edit: null,
      },
    ];
    mockGetSettingsMenu.mockResolvedValue(m);
    render(<SettingsView view="menu" />);

    await screen.findByRole('heading', { name: /^Effort presets and model modes/ });
    await openGroup('Effort presets and model modes');
    const row = (await screen.findByText('effort.level.deep')).closest('[data-testid="setting-row"]') as HTMLElement;
    expect(within(row).getByText('sub_agent_rounds')).toBeInTheDocument();
    expect(within(row).getByText('20')).toBeInTheDocument();
    expect(within(row).getByText('baseline')).toBeInTheDocument();
  });
});

describe('accordion groups (FRE-1710)', () => {
  it('AC-1: closes every group at first render, and a click opens only that group', async () => {
    mockGetSettingsMenu.mockResolvedValue(menu());
    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^Budgets and cost/ });

    for (const title of GROUP_TITLES) {
      expect(header(title)).toHaveAttribute('aria-expanded', 'false');
    }
    expect(screen.queryAllByTestId('setting-row')).toHaveLength(0);

    await openGroup('Budgets and cost');

    expect(header('Budgets and cost')).toHaveAttribute('aria-expanded', 'true');
    expect(screen.getAllByTestId('setting-row')).toHaveLength(1);
    expect(screen.getByText('budgets.example')).toBeInTheDocument();
    for (const title of GROUP_TITLES.filter((t) => t !== 'Budgets and cost')) {
      expect(header(title)).toHaveAttribute('aria-expanded', 'false');
    }
  });

  it('AC-1: a second click closes the group', async () => {
    mockGetSettingsMenu.mockResolvedValue(menu());
    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^Budgets and cost/ });

    await openGroup('Budgets and cost');
    await openGroup('Budgets and cost');

    expect(header('Budgets and cost')).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('budgets.example')).not.toBeInTheDocument();
  });

  it('keeps the other sections as they were when one section toggles', async () => {
    mockGetSettingsMenu.mockResolvedValue(menu());
    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^Budgets and cost/ });

    await openGroup('Budgets and cost');
    await openGroup('Memory and recall');
    await openGroup('Budgets and cost');

    expect(screen.queryByText('budgets.example')).not.toBeInTheDocument();
    expect(screen.getByText('memory.example')).toBeInTheDocument();
    expect(header('Memory and recall')).toHaveAttribute('aria-expanded', 'true');
  });

  it('AC-1: closes the System group at first render too', async () => {
    mockGetSettingsMenu.mockResolvedValue(menu());
    render(<SettingsView view="system" />);
    await screen.findByRole('heading', { level: 2, name: /^System/ });

    expect(header('System')).toHaveAttribute('aria-expanded', 'false');
    expect(screen.queryByText('setting.neo4j_password')).not.toBeInTheDocument();

    await openGroup('System');
    expect(screen.getByText('setting.neo4j_password')).toBeInTheDocument();
  });

  it('AC-2: shows the group name with its parameter count in the header', async () => {
    const m = menu();
    m.groups[4].rows = [
      ...m.groups[4].rows,
      { ...m.groups[4].rows[0], name: 'budgets.second' },
      { ...m.groups[4].rows[0], name: 'budgets.third' },
    ];
    mockGetSettingsMenu.mockResolvedValue(m);
    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^Budgets and cost/ });

    expect(header('Budgets and cost')).toHaveTextContent('Budgets and cost (3)');
    expect(header('Memory and recall')).toHaveTextContent('Memory and recall (1)');
  });

  it('AC-2: marks a group that holds a saved choice, while the group is closed', async () => {
    mockGetSettingsMenu.mockResolvedValue(withSavedChoice());
    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^Saved choices/ });

    expect(header('Saved choices')).toHaveAttribute('aria-expanded', 'false');
    expect(header('Saved choices')).toHaveTextContent('Saved choices (2)');
    expect(header('Saved choices')).toHaveTextContent('saved choice');
    expect(header('Budgets and cost')).not.toHaveTextContent('saved choice');
  });

  it('AC-2: shows a count of saved choices when a group holds more than one', async () => {
    const m = withSavedChoice();
    m.groups[1].rows[1] = { ...m.groups[1].rows[1], clearable: true, value: 'x', source: 'database row' };
    mockGetSettingsMenu.mockResolvedValue(m);
    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^Saved choices/ });

    expect(header('Saved choices')).toHaveTextContent('2 saved choices');
  });

  it('AC-2: drops the marker after Clear removes the last saved choice', async () => {
    mockGetSettingsMenu.mockResolvedValueOnce(withSavedChoice());
    mockClear.mockResolvedValue({ constraint_name: 'artifact_builder', cleared: true });
    mockGetSettingsMenu.mockResolvedValueOnce(menu());
    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^Saved choices/ });

    await openGroup('Saved choices');
    await userEvent.click(screen.getByRole('button', { name: 'Clear' }));

    await waitFor(() => expect(header('Saved choices')).not.toHaveTextContent('saved choice'));
    expect(header('Saved choices')).toHaveAttribute('aria-expanded', 'true');
  });

  it('AC-2: says in the header that a group could not be loaded', async () => {
    const m = menu();
    m.groups[4] = { ...m.groups[4], rows: [], error: 'unavailable' };
    mockGetSettingsMenu.mockResolvedValue(m);
    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^Budgets and cost/ });

    expect(header('Budgets and cost')).toHaveTextContent(/unavailable/i);
    expect(header('Budgets and cost')).not.toHaveTextContent('(0)');
  });

  it('AC-3: makes each header a focusable button that carries aria-expanded', async () => {
    mockGetSettingsMenu.mockResolvedValue(menu());
    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^Budgets and cost/ });

    for (const title of GROUP_TITLES) {
      const button = header(title);
      expect(button.tagName).toBe('BUTTON');
      expect(button).toHaveAttribute('type', 'button');
      expect(button).toHaveAttribute('aria-expanded', 'false');
      button.focus();
      expect(button).toHaveFocus();
    }
    await openGroup('Budgets and cost');
    expect(header('Budgets and cost')).toHaveAttribute('aria-expanded', 'true');
  });

  it('AC-3: opens and closes a focused header with Enter and with Space', async () => {
    mockGetSettingsMenu.mockResolvedValue(menu());
    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^Budgets and cost/ });

    header('Budgets and cost').focus();
    await userEvent.keyboard('{Enter}');
    expect(header('Budgets and cost')).toHaveAttribute('aria-expanded', 'true');
    await userEvent.keyboard(' ');
    expect(header('Budgets and cost')).toHaveAttribute('aria-expanded', 'false');
  });
});

describe('edit a role binding and a saved choice (FRE-1722)', () => {
  function editableMenu(): SettingsMenu {
    const m = menu();
    m.groups[1] = {
      ...m.groups[1],
      rows: [
        {
          name: 'saved_choice.tool_iteration_limit',
          value: 'continue_10',
          source: 'database row',
          overrides: { value: 'decision card (safe default: finish_now)', source: 'code default' },
          clearable: true,
          note: null,
          file_default: { value: 'decision card (safe default: finish_now)', source: 'code default' },
          edit: { kind: 'saved_choice', options: ['continue_10', 'finish_now', 'always_pause'], current: 'continue_10' },
        },
      ],
    };
    m.groups[2] = {
      ...m.groups[2],
      rows: [
        {
          name: 'role.compressor',
          value: 'claude_sonnet',
          source: 'override',
          overrides: null,
          clearable: true,
          note: 'pinned',
          file_default: { value: 'gpt-5.4-mini', source: 'config file' },
          edit: { kind: 'role_binding', options: ['inherit', 'gpt-5.4-mini', 'claude_sonnet'], current: 'claude_sonnet' },
        },
      ],
    };
    return m;
  }

  async function openRow(group: string, name: string): Promise<HTMLElement> {
    await screen.findByRole('heading', { name: new RegExp(`^${group}`) });
    await openGroup(group);
    return (await screen.findByText(name)).closest('[data-testid="setting-row"]') as HTMLElement;
  }

  it('AC-4: a role binding shows the control, the live value, the file default, the source and Clear', async () => {
    mockGetSettingsMenu.mockResolvedValue(editableMenu());
    render(<SettingsView view="menu" />);
    const row = await openRow('Models per role', 'role.compressor');

    const select = within(row).getByRole('combobox', { name: /role\.compressor/ });
    expect(select).toHaveValue('claude_sonnet');
    expect(within(select).getAllByRole('option').map((o) => o.textContent)).toEqual([
      'inherit',
      'gpt-5.4-mini',
      'claude_sonnet',
    ]);
    expect(within(row).getByTestId('setting-live-value')).toHaveTextContent('claude_sonnet');
    expect(within(row).getByTestId('setting-file-default')).toHaveTextContent('file default gpt-5.4-mini (config file)');
    expect(within(row).getByText('override')).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: 'Clear' })).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('AC-4: a saved choice shows the control, the live value, the default and Clear', async () => {
    mockGetSettingsMenu.mockResolvedValue(editableMenu());
    render(<SettingsView view="menu" />);
    const row = await openRow('Saved choices', 'saved_choice.tool_iteration_limit');

    expect(within(row).getByRole('combobox', { name: /tool_iteration_limit/ })).toHaveValue('continue_10');
    expect(within(row).getByTestId('setting-live-value')).toHaveTextContent('continue_10');
    expect(within(row).getByTestId('setting-file-default')).toHaveTextContent(
      'default decision card (safe default: finish_now) (code default)',
    );
    expect(within(row).getByText('database row')).toBeInTheDocument();
    expect(within(row).getByRole('button', { name: 'Clear' })).toBeInTheDocument();
  });

  it('does not repeat the default in an "overrides" line', async () => {
    mockGetSettingsMenu.mockResolvedValue(editableMenu());
    render(<SettingsView view="menu" />);
    const row = await openRow('Saved choices', 'saved_choice.tool_iteration_limit');
    expect(within(row).queryByText(/^overrides/)).not.toBeInTheDocument();
  });

  it('saves a role binding through the override API, then reloads', async () => {
    mockGetSettingsMenu.mockResolvedValue(editableMenu());
    mockSetOverride.mockResolvedValue({ parameter: 'role.compressor', value: 'gpt-5.4-mini' });
    render(<SettingsView view="menu" />);
    const row = await openRow('Models per role', 'role.compressor');

    await userEvent.selectOptions(within(row).getByRole('combobox'), 'gpt-5.4-mini');
    await userEvent.click(within(row).getByRole('button', { name: 'Save' }));

    expect(mockSetOverride).toHaveBeenCalledWith('role.compressor', 'gpt-5.4-mini');
    expect(mockSetPreference).not.toHaveBeenCalled();
    await waitFor(() => expect(mockGetSettingsMenu).toHaveBeenCalledTimes(2));
  });

  it('saves a saved choice through the preference API, then reloads', async () => {
    mockGetSettingsMenu.mockResolvedValue(editableMenu());
    mockSetPreference.mockResolvedValue({ constraint_name: 'tool_iteration_limit', preferred_action: 'finish_now' });
    render(<SettingsView view="menu" />);
    const row = await openRow('Saved choices', 'saved_choice.tool_iteration_limit');

    await userEvent.selectOptions(within(row).getByRole('combobox'), 'finish_now');
    await userEvent.click(within(row).getByRole('button', { name: 'Save' }));

    expect(mockSetPreference).toHaveBeenCalledWith('tool_iteration_limit', 'finish_now');
    expect(mockSetOverride).not.toHaveBeenCalled();
    await waitFor(() => expect(mockGetSettingsMenu).toHaveBeenCalledTimes(2));
  });

  it('shows the reason when the API refuses an edit', async () => {
    mockGetSettingsMenu.mockResolvedValue(editableMenu());
    mockSetOverride.mockRejectedValue(new Error('the provider of gpt-5.4-mini has no credential on this gateway'));
    render(<SettingsView view="menu" />);
    const row = await openRow('Models per role', 'role.compressor');

    await userEvent.selectOptions(within(row).getByRole('combobox'), 'gpt-5.4-mini');
    await userEvent.click(within(row).getByRole('button', { name: 'Save' }));

    expect(await within(row).findByRole('alert')).toHaveTextContent(/no credential on this gateway/);
    expect(within(row).getByRole('combobox')).toHaveValue('gpt-5.4-mini');
    expect(within(row).getByTestId('setting-live-value')).toHaveTextContent('claude_sonnet');
  });

  it('clears a role binding through the override API, not the preference API', async () => {
    mockGetSettingsMenu.mockResolvedValue(editableMenu());
    mockClearOverride.mockResolvedValue({ parameter: 'role.compressor', cleared: true });
    render(<SettingsView view="menu" />);
    const row = await openRow('Models per role', 'role.compressor');

    await userEvent.click(within(row).getByRole('button', { name: 'Clear' }));

    expect(mockClearOverride).toHaveBeenCalledWith('role.compressor');
    expect(mockClear).not.toHaveBeenCalled();
    await waitFor(() => expect(mockGetSettingsMenu).toHaveBeenCalledTimes(2));
  });

  it('shows the new value in the control after the menu reloads', async () => {
    const after = editableMenu();
    after.groups[2].rows[0] = {
      ...after.groups[2].rows[0],
      value: 'gpt-5.4-mini',
      edit: { kind: 'role_binding', options: ['inherit', 'gpt-5.4-mini', 'claude_sonnet'], current: 'gpt-5.4-mini' },
    };
    mockGetSettingsMenu.mockResolvedValueOnce(editableMenu()).mockResolvedValueOnce(after);
    mockClearOverride.mockResolvedValue({ parameter: 'role.compressor', cleared: true });
    render(<SettingsView view="menu" />);
    const row = await openRow('Models per role', 'role.compressor');

    await userEvent.click(within(row).getByRole('button', { name: 'Clear' }));

    await waitFor(() => expect(within(row).getByRole('combobox')).toHaveValue('gpt-5.4-mini'));
    expect(within(row).getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('marks an override apart from a saved choice in the closed header', async () => {
    mockGetSettingsMenu.mockResolvedValue(editableMenu());
    render(<SettingsView view="menu" />);
    await screen.findByRole('heading', { name: /^Models per role/ });

    expect(header('Models per role')).toHaveTextContent('override');
    expect(header('Models per role')).not.toHaveTextContent('saved choice');
    expect(header('Saved choices')).toHaveTextContent('saved choice');
  });
});

describe('edit an effort preset and a budget cap (FRE-1723)', () => {
  const STANDARD = { expansion: true, sub_agent_rounds: 12, planner_policy: 'p2', primary_mode: null };
  const FAST = { expansion: false, sub_agent_rounds: null, planner_policy: null, primary_mode: 'fast' };
  const CHOICES = { planner_policy: ['baseline', 'p2'], primary_mode: ['fast', 'deep'] };

  function editableMenu(): SettingsMenu {
    const m = menu();
    m.groups[3] = {
      ...m.groups[3],
      rows: [
        {
          name: 'effort.default_level',
          value: 'standard',
          source: 'config file',
          overrides: null,
          clearable: false,
          note: null,
          file_default: { value: 'standard', source: 'config file' },
          edit: { kind: 'effort_default_level', options: ['fast', 'standard', 'deep'], current: 'standard' },
        },
        {
          name: 'effort.level.standard',
          value: STANDARD,
          source: 'override',
          overrides: null,
          clearable: true,
          note: null,
          file_default: { value: { ...STANDARD, sub_agent_rounds: 20 }, source: 'config file' },
          edit: { kind: 'effort_preset', options: [], current: null, preset_choices: CHOICES },
        },
        {
          name: 'effort.level.fast',
          value: FAST,
          source: 'config file',
          overrides: null,
          clearable: false,
          note: null,
          file_default: { value: FAST, source: 'config file' },
          edit: { kind: 'effort_preset', options: [], current: null, preset_choices: CHOICES },
        },
      ],
    };
    m.groups[4] = {
      ...m.groups[4],
      rows: [
        {
          name: 'cap.daily.main_inference',
          value: 2.5,
          source: 'override',
          overrides: null,
          clearable: true,
          note: null,
          file_default: { value: 10, source: 'config file' },
          edit: { kind: 'budget_cap', options: [], current: '2.5' },
        },
      ],
    };
    return m;
  }

  async function openRow(group: string, name: string): Promise<HTMLElement> {
    await screen.findByRole('heading', { name: new RegExp(`^${group}`) });
    await openGroup(group);
    return (await screen.findByText(name)).closest('[data-testid="setting-row"]') as HTMLElement;
  }

  function save(row: HTMLElement): HTMLElement {
    return within(row).getByRole('button', { name: 'Save' });
  }

  it('a cap row shows a number input, the live value, the file default and Clear', async () => {
    mockGetSettingsMenu.mockResolvedValue(editableMenu());
    render(<SettingsView view="menu" />);
    const row = await openRow('Budgets and cost', 'cap.daily.main_inference');

    expect(within(row).getByRole('spinbutton', { name: /cap\.daily\.main_inference/ })).toHaveValue(2.5);
    expect(within(row).getByTestId('setting-live-value')).toHaveTextContent('2.5');
    expect(within(row).getByTestId('setting-file-default')).toHaveTextContent('file default 10 (config file)');
    expect(within(row).getByRole('button', { name: 'Clear' })).toBeInTheDocument();
    expect(save(row)).toBeDisabled();
  });

  it('saves a cap as a number through the override API, then reloads', async () => {
    mockGetSettingsMenu.mockResolvedValue(editableMenu());
    mockSetOverride.mockResolvedValue({ parameter: 'cap.daily.main_inference', value: 1.25 });
    render(<SettingsView view="menu" />);
    const row = await openRow('Budgets and cost', 'cap.daily.main_inference');

    const input = within(row).getByRole('spinbutton');
    await userEvent.clear(input);
    expect(save(row)).toBeDisabled();
    await userEvent.type(input, '1.25');
    await userEvent.click(save(row));

    expect(mockSetOverride).toHaveBeenCalledWith('cap.daily.main_inference', 1.25);
    await waitFor(() => expect(mockGetSettingsMenu).toHaveBeenCalledTimes(2));
  });

  it('shows the reason when the API refuses a cap, and keeps the draft', async () => {
    mockGetSettingsMenu.mockResolvedValue(editableMenu());
    mockSetOverride.mockRejectedValue(new Error('Input should be greater than or equal to 0'));
    render(<SettingsView view="menu" />);
    const row = await openRow('Budgets and cost', 'cap.daily.main_inference');

    const input = within(row).getByRole('spinbutton');
    await userEvent.clear(input);
    await userEvent.type(input, '-1');
    await userEvent.click(save(row));

    expect(await within(row).findByRole('alert')).toHaveTextContent(/greater than or equal to 0/);
    expect(input).toHaveValue(-1);
    expect(within(row).getByTestId('setting-live-value')).toHaveTextContent('2.5');
  });

  it('a preset row shows its four fields from the live value', async () => {
    mockGetSettingsMenu.mockResolvedValue(editableMenu());
    render(<SettingsView view="menu" />);
    const row = await openRow('Effort presets', 'effort.level.standard');

    expect(within(row).getByRole('checkbox', { name: /expansion/ })).toBeChecked();
    expect(within(row).getByRole('spinbutton', { name: /sub_agent_rounds/ })).toHaveValue(12);
    expect(within(row).getByRole('combobox', { name: /planner_policy/ })).toHaveValue('p2');
    expect(within(row).getByRole('combobox', { name: /primary_mode/ })).toHaveValue('');
    expect(within(row).getByTestId('setting-file-default')).toHaveTextContent('file default');
    expect(within(row).getByRole('button', { name: 'Clear' })).toBeInTheDocument();
    expect(save(row)).toBeDisabled();
  });

  it('saves the whole preset through the override API, then reloads', async () => {
    mockGetSettingsMenu.mockResolvedValue(editableMenu());
    mockSetOverride.mockResolvedValue({ parameter: 'effort.level.standard', value: STANDARD });
    render(<SettingsView view="menu" />);
    const row = await openRow('Effort presets', 'effort.level.standard');

    const rounds = within(row).getByRole('spinbutton', { name: /sub_agent_rounds/ });
    await userEvent.clear(rounds);
    await userEvent.type(rounds, '5');
    await userEvent.selectOptions(within(row).getByRole('combobox', { name: /primary_mode/ }), 'deep');
    await userEvent.click(save(row));

    expect(mockSetOverride).toHaveBeenCalledWith('effort.level.standard', {
      expansion: true,
      sub_agent_rounds: 5,
      planner_policy: 'p2',
      primary_mode: 'deep',
    });
    await waitFor(() => expect(mockGetSettingsMenu).toHaveBeenCalledTimes(2));
  });

  it('sends null rounds and a null policy when expansion is off', async () => {
    mockGetSettingsMenu.mockResolvedValue(editableMenu());
    mockSetOverride.mockResolvedValue({ parameter: 'effort.level.standard', value: FAST });
    render(<SettingsView view="menu" />);
    const row = await openRow('Effort presets', 'effort.level.standard');

    await userEvent.click(within(row).getByRole('checkbox', { name: /expansion/ }));
    expect(within(row).getByRole('spinbutton', { name: /sub_agent_rounds/ })).toBeDisabled();
    expect(within(row).getByRole('combobox', { name: /planner_policy/ })).toBeDisabled();
    await userEvent.click(save(row));

    expect(mockSetOverride).toHaveBeenCalledWith('effort.level.standard', {
      expansion: false,
      sub_agent_rounds: null,
      planner_policy: null,
      primary_mode: null,
    });
  });

  it('a preset with no expansion sends null rounds when the owner turns expansion on and leaves them', async () => {
    mockGetSettingsMenu.mockResolvedValue(editableMenu());
    mockSetOverride.mockRejectedValue(new Error("level 'fast' expands, so it needs sub_agent_rounds"));
    render(<SettingsView view="menu" />);
    const row = await openRow('Effort presets', 'effort.level.fast');

    await userEvent.click(within(row).getByRole('checkbox', { name: /expansion/ }));
    await userEvent.click(save(row));

    expect(mockSetOverride).toHaveBeenCalledWith('effort.level.fast', {
      expansion: true,
      sub_agent_rounds: null,
      planner_policy: null,
      primary_mode: 'fast',
    });
    expect(await within(row).findByRole('alert')).toHaveTextContent(/needs sub_agent_rounds/);
  });

  it('the default level uses the select and saves its name', async () => {
    mockGetSettingsMenu.mockResolvedValue(editableMenu());
    mockSetOverride.mockResolvedValue({ parameter: 'effort.default_level', value: 'deep' });
    render(<SettingsView view="menu" />);
    const row = await openRow('Effort presets', 'effort.default_level');

    await userEvent.selectOptions(within(row).getByRole('combobox'), 'deep');
    await userEvent.click(save(row));

    expect(mockSetOverride).toHaveBeenCalledWith('effort.default_level', 'deep');
  });

  it('the draft follows the live value after the menu reloads', async () => {
    const after = editableMenu();
    after.groups[3].rows[1] = {
      ...after.groups[3].rows[1],
      value: { ...STANDARD, sub_agent_rounds: 20 },
    };
    after.groups[4].rows[0] = {
      ...after.groups[4].rows[0],
      value: 10,
      edit: { kind: 'budget_cap', options: [], current: '10' },
    };
    mockGetSettingsMenu.mockResolvedValueOnce(editableMenu()).mockResolvedValue(after);
    mockClearOverride.mockResolvedValue({ parameter: 'x', cleared: true });
    render(<SettingsView view="menu" />);

    const cap = await openRow('Budgets and cost', 'cap.daily.main_inference');
    const preset = await openRow('Effort presets', 'effort.level.standard');
    await userEvent.click(within(cap).getByRole('button', { name: 'Clear' }));

    await waitFor(() => expect(within(cap).getByRole('spinbutton')).toHaveValue(10));
    expect(within(preset).getByRole('spinbutton', { name: /sub_agent_rounds/ })).toHaveValue(20);
    expect(save(cap)).toBeDisabled();
    expect(save(preset)).toBeDisabled();
  });
});

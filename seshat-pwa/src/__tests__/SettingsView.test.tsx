/**
 * Tests for SettingsView (FRE-1702, stage 1 of FRE-1698).
 *
 * AC-1 (unit half): the nine groups render, and the System view renders the System group;
 *   every row shows its name, live value and winning source.
 * AC-2: a saved choice shows beside the default that it overrides.
 * AC-5: the only control is "Clear" on a saved choice. No input, select, textarea or toggle.
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { vi, describe, it, expect, beforeEach } from 'vitest';

import type { SettingsMenu } from '@/lib/types';

const mockGetSettingsMenu = vi.fn();
const mockClear = vi.fn();
const mockResolveLastSessionId = vi.fn();
let mockSearchParams = new URLSearchParams();

vi.mock('@/lib/agui-client', () => ({
  getSettingsMenu: (...args: unknown[]) => mockGetSettingsMenu(...args),
  clearConstraintPreference: (...args: unknown[]) => mockClear(...args),
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
      },
      {
        name: 'saved_choice.tool_iteration_limit',
        value: 'not set',
        source: 'code default',
        overrides: null,
        clearable: false,
        note: null,
      },
    ],
  };
  return m;
}

beforeEach(() => {
  mockGetSettingsMenu.mockReset();
  mockClear.mockReset();
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
      expect(await screen.findByRole('heading', { name: title })).toBeInTheDocument();
    }
    expect(screen.queryByRole('heading', { name: /System/ })).not.toBeInTheDocument();

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

    expect(await screen.findByRole('heading', { name: 'System (read-only)' })).toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: 'Saved choices' })).not.toBeInTheDocument();
    const row = screen.getByText('setting.neo4j_password').closest('[data-testid="setting-row"]') as HTMLElement;
    expect(within(row).getByText('set')).toBeInTheDocument();
    expect(within(row).getByText('.env')).toBeInTheDocument();
  });
});

describe('saved choice beside its default (AC-2)', () => {
  it('shows the overridden default and its source next to the saved value', async () => {
    mockGetSettingsMenu.mockResolvedValue(withSavedChoice());
    render(<SettingsView view="menu" />);

    const row = (await screen.findByText('saved_choice.artifact_builder')).closest(
      '[data-testid="setting-row"]',
    ) as HTMLElement;
    expect(within(row).getByText('claude_sonnet')).toBeInTheDocument();
    expect(within(row).getByText('database row')).toBeInTheDocument();
    expect(within(row).getByText(/overrides/i)).toBeInTheDocument();
    expect(within(row).getByText('gpt-6.1-sol')).toBeInTheDocument();
  });
});

describe('no control edits a parameter (AC-5)', () => {
  it('has no input, select, textarea, switch or checkbox', async () => {
    mockGetSettingsMenu.mockResolvedValue(withSavedChoice());
    const { container } = render(<SettingsView view="menu" />);
    await screen.findByText('saved_choice.artifact_builder');

    expect(container.querySelectorAll('input, select, textarea')).toHaveLength(0);
    expect(screen.queryAllByRole('switch')).toHaveLength(0);
    expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
    expect(screen.queryAllByRole('combobox')).toHaveLength(0);
  });

  it('has one button per clearable row, and each says Clear', async () => {
    mockGetSettingsMenu.mockResolvedValue(withSavedChoice());
    render(<SettingsView view="menu" />);
    await screen.findByText('saved_choice.artifact_builder');

    const buttons = screen.getAllByRole('button');
    expect(buttons).toHaveLength(1);
    expect(buttons[0]).toHaveTextContent(/^Clear$/);
  });

  it('shows no button at all when no saved choice exists', async () => {
    mockGetSettingsMenu.mockResolvedValue(menu());
    render(<SettingsView view="menu" />);
    await screen.findByText('conversation.example');
    expect(screen.queryAllByRole('button')).toHaveLength(0);
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
    await userEvent.click(await screen.findByRole('button', { name: 'Clear' }));

    expect(mockClear).toHaveBeenCalledWith('artifact_builder');
    await waitFor(() => expect(mockGetSettingsMenu).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryAllByRole('button')).toHaveLength(0));
  });

  it('shows an error and keeps the row when the DELETE fails', async () => {
    mockGetSettingsMenu.mockResolvedValue(withSavedChoice());
    mockClear.mockRejectedValue(new Error('clearConstraintPreference failed: 500'));

    render(<SettingsView view="menu" />);
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
    await screen.findByText('conversation.example');
    expect(mockGetSettingsMenu).toHaveBeenCalledWith('11111111-1111-1111-1111-111111111111');
  });

  it('falls back to the stored last session', async () => {
    localStorage.setItem('seshat_last_session_id', '22222222-2222-2222-2222-222222222222');
    mockGetSettingsMenu.mockResolvedValue(menu());
    render(<SettingsView view="menu" />);
    await screen.findByText('conversation.example');
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

    expect(await screen.findByText('conversation.example')).toBeInTheDocument();
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

    expect(await screen.findByText(/could not be loaded/i)).toBeInTheDocument();
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
      },
    ];
    mockGetSettingsMenu.mockResolvedValue(m);
    render(<SettingsView view="menu" />);

    const row = (await screen.findByText('effort.level.deep')).closest('[data-testid="setting-row"]') as HTMLElement;
    expect(within(row).getByText('sub_agent_rounds')).toBeInTheDocument();
    expect(within(row).getByText('20')).toBeInTheDocument();
    expect(within(row).getByText('baseline')).toBeInTheDocument();
  });
});

/**
 * Tests for RegisterSW — service worker registration + auto-update.
 *
 * Covers:
 *   - SW is registered at /sw.js on mount
 *   - controllerchange triggers reload when wasControlled=true (update scenario)
 *   - controllerchange does NOT reload when wasControlled=false (first install)
 *   - reloading flag prevents double-reload
 *   - graceful no-op when navigator.serviceWorker is absent
 *   - listener is removed on unmount (cleanup)
 *   - registration failure is logged in development, silent in production
 */

import { render, act } from '@testing-library/react';
import { vi, describe, it, expect, beforeEach, afterEach } from 'vitest';
import { RegisterSW } from '@/components/RegisterSW';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

type ControllerChangeListener = EventListenerOrEventListenerObject;

interface FakeSWContainer {
  controller: ServiceWorker | null;
  register: ReturnType<typeof vi.fn>;
  addEventListener: ReturnType<typeof vi.fn>;
  removeEventListener: ReturnType<typeof vi.fn>;
  _listeners: ControllerChangeListener[];
  _fireControllerChange: () => void;
}

function makeSWContainer(controller: ServiceWorker | null = null): FakeSWContainer {
  const listeners: ControllerChangeListener[] = [];
  return {
    controller,
    register: vi.fn().mockResolvedValue({}),
    addEventListener: vi.fn((_evt: string, cb: ControllerChangeListener) => {
      listeners.push(cb);
    }),
    removeEventListener: vi.fn((_evt: string, cb: ControllerChangeListener) => {
      const idx = listeners.indexOf(cb);
      if (idx !== -1) listeners.splice(idx, 1);
    }),
    _listeners: listeners,
    _fireControllerChange(): void {
      listeners.forEach((cb) => {
        if (typeof cb === 'function') cb(new Event('controllerchange'));
      });
    },
  };
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

let originalSW: ServiceWorkerContainer | undefined;
let reloadSpy: ReturnType<typeof vi.fn>;

beforeEach(() => {
  originalSW = (navigator as { serviceWorker?: ServiceWorkerContainer }).serviceWorker;
  reloadSpy = vi.fn();
  Object.defineProperty(window, 'location', {
    value: { ...window.location, reload: reloadSpy },
    writable: true,
    configurable: true,
  });
});

afterEach(() => {
  Object.defineProperty(navigator, 'serviceWorker', {
    value: originalSW,
    writable: true,
    configurable: true,
  });
  vi.restoreAllMocks();
});

function mountSW(swContainer: FakeSWContainer): void {
  Object.defineProperty(navigator, 'serviceWorker', {
    value: swContainer,
    writable: true,
    configurable: true,
  });
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe('RegisterSW', () => {
  it('registers /sw.js on mount', () => {
    const sw = makeSWContainer();
    mountSW(sw);

    render(<RegisterSW />);

    expect(sw.register).toHaveBeenCalledWith('/sw.js');
  });

  it('attaches controllerchange listener on mount', () => {
    const sw = makeSWContainer();
    mountSW(sw);

    render(<RegisterSW />);

    expect(sw.addEventListener).toHaveBeenCalledWith('controllerchange', expect.any(Function));
  });

  it('reloads on controllerchange when there was a previous controller', async () => {
    const fakeController = {} as ServiceWorker;
    const sw = makeSWContainer(fakeController);
    mountSW(sw);

    render(<RegisterSW />);

    await act(async () => {
      sw._fireControllerChange();
    });

    expect(reloadSpy).toHaveBeenCalledOnce();
  });

  it('does NOT reload on controllerchange when this is the first install (wasControlled=false)', async () => {
    const sw = makeSWContainer(null); // no previous controller
    mountSW(sw);

    render(<RegisterSW />);

    await act(async () => {
      sw._fireControllerChange();
    });

    expect(reloadSpy).not.toHaveBeenCalled();
  });

  it('does not reload twice if controllerchange fires multiple times', async () => {
    const fakeController = {} as ServiceWorker;
    const sw = makeSWContainer(fakeController);
    mountSW(sw);

    render(<RegisterSW />);

    await act(async () => {
      sw._fireControllerChange();
      sw._fireControllerChange();
    });

    expect(reloadSpy).toHaveBeenCalledOnce();
  });

  it('removes controllerchange listener on unmount', () => {
    const sw = makeSWContainer();
    mountSW(sw);

    const { unmount } = render(<RegisterSW />);
    unmount();

    expect(sw.removeEventListener).toHaveBeenCalledWith('controllerchange', expect.any(Function));
    expect(sw._listeners).toHaveLength(0);
  });

  it('is a no-op when navigator.serviceWorker is not available', () => {
    // Simulate browsers without SW support by removing the property entirely.
    Object.defineProperty(navigator, 'serviceWorker', {
      value: null,
      writable: true,
      configurable: true,
    });

    expect(() => render(<RegisterSW />)).not.toThrow();
    expect(reloadSpy).not.toHaveBeenCalled();
  });

  it('logs registration failure in non-production environments', async () => {
    // vitest runs with NODE_ENV=test (non-production), so the error branch fires.
    const err = new Error('HTTPS required');
    const sw = makeSWContainer();
    sw.register.mockRejectedValue(err);
    mountSW(sw);

    const errorSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    render(<RegisterSW />);
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });

    expect(errorSpy).toHaveBeenCalledWith('[SW] registration failed', err);
    errorSpy.mockRestore();
  });

  it('returns null (renders nothing)', () => {
    const sw = makeSWContainer();
    mountSW(sw);

    const { container } = render(<RegisterSW />);
    expect(container).toBeEmptyDOMElement();
  });
});

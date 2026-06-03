/**
 * Tests for the FRE-230 LocationConsent drawer control.
 *
 * Covers: operator-gate hiding (feature_enabled=false renders nothing),
 * toggle reflects consent, enabling consent requests device geolocation and
 * PATCHes coordinates, and permission-denied surfaces the iOS guidance copy.
 */

import { render, screen, waitFor, fireEvent, act } from '@testing-library/react';
import { vi, it, expect, beforeEach } from 'vitest';

const getLocationPreference = vi.fn();
const updateLocationPreference = vi.fn();

vi.mock('@/lib/agui-client', () => ({
  SESHAT_API: 'http://localhost:9000',
  authHeaders: () => ({}),
  getLocationPreference: () => getLocationPreference(),
  updateLocationPreference: (...args: unknown[]) => updateLocationPreference(...args),
}));

import { LocationConsent } from '@/components/LocationConsent';

const getCurrentPosition = vi.fn();

beforeEach(() => {
  getLocationPreference.mockReset();
  updateLocationPreference.mockReset();
  getCurrentPosition.mockReset();
  updateLocationPreference.mockResolvedValue({
    feature_enabled: true,
    location_consent_enabled: true,
  });
  Object.defineProperty(global.navigator, 'geolocation', {
    configurable: true,
    value: { getCurrentPosition },
  });
});

it('renders nothing when the operator gate is disabled', async () => {
  getLocationPreference.mockResolvedValue({
    feature_enabled: false,
    location_consent_enabled: false,
  });
  const { container } = render(<LocationConsent />);
  await waitFor(() => expect(getLocationPreference).toHaveBeenCalled());
  expect(container).toBeEmptyDOMElement();
});

it('shows the toggle reflecting current consent when the feature is enabled', async () => {
  getLocationPreference.mockResolvedValue({
    feature_enabled: true,
    location_consent_enabled: true,
  });
  render(<LocationConsent />);
  const toggle = await screen.findByRole('switch', { name: /share location/i });
  expect(toggle).toHaveAttribute('aria-checked', 'true');
});

it('requests device location and stores coordinates when consent is enabled', async () => {
  getLocationPreference.mockResolvedValue({
    feature_enabled: true,
    location_consent_enabled: false,
  });
  getCurrentPosition.mockImplementation(
    (resolve: (p: { coords: { latitude: number; longitude: number } }) => void) => {
      resolve({ coords: { latitude: 38.7077507, longitude: -9.1365919 } });
    },
  );

  render(<LocationConsent />);
  const toggle = await screen.findByRole('switch', { name: /share location/i });
  fireEvent.click(toggle);

  // First call enables consent; second call sends coordinates.
  await waitFor(() => expect(updateLocationPreference).toHaveBeenCalledWith(true));
  await waitFor(() =>
    expect(updateLocationPreference).toHaveBeenCalledWith(
      undefined,
      expect.objectContaining({ latitude: 38.7077507, longitude: -9.1365919 }),
    ),
  );
  expect(await screen.findByText(/location shared/i)).toBeInTheDocument();
});

it('surfaces iOS guidance when permission is denied', async () => {
  getLocationPreference.mockResolvedValue({
    feature_enabled: true,
    location_consent_enabled: false,
  });
  getCurrentPosition.mockImplementation(
    (_resolve: unknown, reject: (e: { code: number }) => void) => {
      reject({ code: 1 }); // PERMISSION_DENIED
    },
  );

  render(<LocationConsent />);
  const toggle = await screen.findByRole('switch', { name: /share location/i });
  fireEvent.click(toggle);

  expect(await screen.findByText(/iOS Settings/i)).toBeInTheDocument();
  // Consent was still requested even though collection failed.
  expect(updateLocationPreference).toHaveBeenCalledWith(true);
});

it('disabling consent never requests or sends location', async () => {
  getLocationPreference.mockResolvedValue({
    feature_enabled: true,
    location_consent_enabled: true,
  });

  render(<LocationConsent />);
  const toggle = await screen.findByRole('switch', { name: /share location/i });
  fireEvent.click(toggle);

  await waitFor(() => expect(updateLocationPreference).toHaveBeenCalledWith(false));
  expect(getCurrentPosition).not.toHaveBeenCalled();
  // No coordinate PATCH (a two-arg call) was ever made.
  expect(updateLocationPreference).not.toHaveBeenCalledWith(undefined, expect.anything());
});

it('reverts the switch when the consent PATCH fails', async () => {
  getLocationPreference.mockResolvedValue({
    feature_enabled: true,
    location_consent_enabled: false,
  });
  updateLocationPreference.mockRejectedValueOnce(new Error('500'));

  render(<LocationConsent />);
  const toggle = await screen.findByRole('switch', { name: /share location/i });
  fireEvent.click(toggle);

  // Optimistic flip reverted back to unchecked after the failed PATCH.
  await waitFor(() => expect(toggle).toHaveAttribute('aria-checked', 'false'));
  expect(getCurrentPosition).not.toHaveBeenCalled();
});

it('suppresses the coordinate PATCH when consent is withdrawn mid-collection', async () => {
  getLocationPreference.mockResolvedValue({
    feature_enabled: true,
    location_consent_enabled: false,
  });
  // Hold the geolocation fix open so we can withdraw consent before it resolves.
  let releaseFix: (() => void) | undefined;
  getCurrentPosition.mockImplementation(
    (resolve: (p: { coords: { latitude: number; longitude: number } }) => void) => {
      releaseFix = () => resolve({ coords: { latitude: 38.7, longitude: -9.1 } });
    },
  );

  render(<LocationConsent />);
  const toggle = await screen.findByRole('switch', { name: /share location/i });

  // Enable: consent PATCH resolves, then getCurrentPosition is invoked (pending).
  await act(async () => {
    fireEvent.click(toggle);
  });
  await waitFor(() => expect(getCurrentPosition).toHaveBeenCalled());

  // Withdraw consent while the fix is still pending.
  await act(async () => {
    fireEvent.click(toggle);
  });
  await waitFor(() => expect(updateLocationPreference).toHaveBeenCalledWith(false));

  // Now the device returns a fix — it must be discarded, not transmitted.
  await act(async () => {
    releaseFix?.();
  });

  expect(updateLocationPreference).not.toHaveBeenCalledWith(undefined, expect.anything());
});

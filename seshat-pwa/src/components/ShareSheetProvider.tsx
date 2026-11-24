/**
 * ShareSheetProvider — hosts the one share sheet at the app root (FRE-1784).
 *
 * The sheet holds a link's URL, which the gateway shows only once. A chat card is a poor owner
 * for that state: a history rehydrate gives every message a new id, which remounts each card and
 * would drop an open sheet, even while its create call runs. Here the sheet lives in the root
 * layout, outside the message tree and outside the markdown `prose` styles.
 */

'use client';

import { createContext, useCallback, useContext, useMemo, useState } from 'react';

import { ShareSheet } from './ShareSheet';

interface ShareTarget {
  artifactId: string;
  title: string | null;
}

interface ShareSheetApi {
  /** Open the share sheet for one item. */
  openShareSheet: (artifactId: string, title: string | null) => void;
}

const ShareSheetContext = createContext<ShareSheetApi | null>(null);

/** Render the share sheet above everything else, and give descendants a way to open it. */
export function ShareSheetProvider({ children }: { children: React.ReactNode }) {
  const [target, setTarget] = useState<ShareTarget | null>(null);

  const openShareSheet = useCallback((artifactId: string, title: string | null) => {
    // One sheet at a time: an open sheet keeps its link until the owner closes it.
    setTarget((current) => current ?? { artifactId, title });
  }, []);
  const close = useCallback(() => setTarget(null), []);
  const api = useMemo(() => ({ openShareSheet }), [openShareSheet]);

  return (
    <ShareSheetContext.Provider value={api}>
      {children}
      {target && (
        <ShareSheet
          key={target.artifactId}
          artifactId={target.artifactId}
          title={target.title}
          onClose={close}
        />
      )}
    </ShareSheetContext.Provider>
  );
}

/**
 * Return the function that opens the share sheet, or `null` outside a ShareSheetProvider.
 *
 * The root layout always provides one. A component rendered on its own, as in a unit test,
 * gets `null` and shows no Share button.
 */
export function useShareSheet(): ShareSheetApi | null {
  return useContext(ShareSheetContext);
}

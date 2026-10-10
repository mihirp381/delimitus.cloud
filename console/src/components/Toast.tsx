import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import { Icon } from './Icon';

interface Toast {
  readonly id: number;
  readonly message: string;
}

type Show = (message: string) => void;

const ToastContext = createContext<Show>(() => undefined);

/** How long a message stays unless it is dismissed. Long enough to read an approval's id. */
export const TOAST_MS = 12_000;
/** The newest few are kept; an older one makes room. */
const KEPT = 3;

/**
 * The region where a finished action says so: "Shared.", "Removed access for…". It is a polite
 * live region, so a screen reader reads each message when it has a moment, and each can be
 * dismissed. A failure is never shown here: it stays beside the control that failed.
 */
export function ToastRegion({ children }: { readonly children: ReactNode }) {
  const [toasts, setToasts] = useState<readonly Toast[]>([]);
  const next = useRef(1);
  const timers = useRef(new Map<number, number>());

  const dismiss = useCallback((id: number) => {
    window.clearTimeout(timers.current.get(id));
    timers.current.delete(id);
    setToasts((list) => list.filter((t) => t.id !== id));
  }, []);

  const show = useCallback<Show>(
    (message) => {
      const id = next.current++;
      setToasts((list) => [...list.slice(-(KEPT - 1)), { id, message }]);
      timers.current.set(
        id,
        window.setTimeout(() => dismiss(id), TOAST_MS),
      );
    },
    [dismiss],
  );

  useEffect(() => {
    const pending = timers.current;
    return () => {
      for (const timer of pending.values()) window.clearTimeout(timer);
      pending.clear();
    };
  }, []);

  const value = useMemo(() => show, [show]);
  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toasts" role="region" aria-label="Notifications" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className="toast">
            <span className="toast-mark" aria-hidden="true">
              <Icon name="check" />
            </span>
            <p>{t.message}</p>
            <button type="button" className="icon-btn" aria-label="Dismiss" onClick={() => dismiss(t.id)}>
              <Icon name="close" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/** Says a finished action in the toast region. Outside the console's shell it does nothing. */
export function useToast(): Show {
  return useContext(ToastContext);
}

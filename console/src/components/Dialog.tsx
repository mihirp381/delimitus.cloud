import { type ReactNode, useEffect, useId, useRef } from 'react';

interface Props {
  readonly open: boolean;
  readonly title: string;
  readonly onClose: () => void;
  readonly children: ReactNode;
}

/** A modal on the native <dialog>: focus stays inside, Escape closes, the page behind is inert. */
export function Dialog({ open, title, onClose, children }: Props) {
  const ref = useRef<HTMLDialogElement>(null);
  const titleId = useId();
  useEffect(() => {
    const dialog = ref.current;
    if (!dialog) return;
    if (open && !dialog.open) dialog.showModal();
    if (!open && dialog.open) dialog.close();
  }, [open]);
  return (
    <dialog ref={ref} className="dialog" aria-labelledby={titleId} onClose={onClose}>
      {open ? (
        <>
          <h2 id={titleId}>{title}</h2>
          {children}
        </>
      ) : null}
    </dialog>
  );
}

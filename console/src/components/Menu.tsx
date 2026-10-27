import { type KeyboardEvent, type ReactNode, useEffect, useId, useRef, useState } from 'react';
import { Button } from './Button';

export interface MenuItem {
  readonly label: string;
  readonly onSelect: () => void;
}

interface Props {
  readonly label: ReactNode;
  readonly items: readonly MenuItem[];
  readonly disabled?: boolean;
}

export function Menu({ label, items, disabled }: Props) {
  const [open, setOpen] = useState(false);
  const buttonId = useId();
  const menuId = useId();
  const wrap = useRef<HTMLDivElement>(null);
  const button = useRef<HTMLButtonElement>(null);

  const entries = () => [...(wrap.current?.querySelectorAll<HTMLButtonElement>('[role="menuitem"]') ?? [])];

  useEffect(() => {
    if (!open) return undefined;
    entries()[0]?.focus();
    const away = (event: PointerEvent) => {
      if (!wrap.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('pointerdown', away);
    return () => document.removeEventListener('pointerdown', away);
  }, [open]);

  function close() {
    setOpen(false);
    button.current?.focus();
  }

  function onKey(event: KeyboardEvent<HTMLDivElement>) {
    const list = entries();
    const at = list.indexOf(document.activeElement as HTMLButtonElement);
    const move = (to: number) => {
      event.preventDefault();
      list[(to + list.length) % list.length]?.focus();
    };
    if (event.key === 'Escape') {
      event.preventDefault();
      close();
    } else if (event.key === 'ArrowDown') move(at + 1);
    else if (event.key === 'ArrowUp') move(at - 1);
    else if (event.key === 'Home') move(0);
    else if (event.key === 'End') move(list.length - 1);
    else if (event.key === 'Tab') setOpen(false);
  }

  function onButtonKey(event: KeyboardEvent<HTMLButtonElement>) {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      setOpen(true);
    }
  }

  return (
    <div className="menu-wrap" ref={wrap}>
      <Button
        ref={button}
        id={buttonId}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        disabled={disabled}
        onClick={() => setOpen((was) => !was)}
        onKeyDown={onButtonKey}
      >
        {label}
      </Button>
      {open ? (
        <div id={menuId} className="menu" role="menu" aria-labelledby={buttonId} onKeyDown={onKey}>
          {items.map((item) => (
            <button
              key={item.label}
              type="button"
              role="menuitem"
              tabIndex={-1}
              className="menu-item"
              onClick={() => {
                close();
                item.onSelect();
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

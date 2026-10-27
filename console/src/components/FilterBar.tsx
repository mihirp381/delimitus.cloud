import { type ReactNode, useId, useState } from 'react';
import { Button } from './Button';

interface Props {
  readonly label: string;
  readonly primary: ReactNode;
  readonly more?: ReactNode;
  readonly defaultOpen?: boolean;
}

export function FilterBar({ label, primary, more, defaultOpen = false }: Props) {
  const [open, setOpen] = useState(defaultOpen);
  const buttonId = useId();
  const panelId = useId();
  return (
    <div className="filter-bar" role="group" aria-label={label}>
      <div className="filter-bar-row">
        {primary}
        {more ? (
          <Button
            id={buttonId}
            aria-expanded={open}
            aria-controls={panelId}
            onClick={() => setOpen((was) => !was)}
          >
            More filters
          </Button>
        ) : null}
      </div>
      {more ? (
        <div id={panelId} className="filter-bar-more" role="region" aria-labelledby={buttonId} hidden={!open}>
          {more}
        </div>
      ) : null}
    </div>
  );
}

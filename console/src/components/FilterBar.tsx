import { type ReactNode, useId, useState } from 'react';
import { Button } from './Button';

interface Props {
  readonly label: string;
  readonly primary: ReactNode;
  readonly more?: ReactNode;
  readonly defaultOpen?: boolean;
  readonly count?: number;
}

export function FilterBar({ label, primary, more, defaultOpen = false, count = 0 }: Props) {
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
            className="btn-sm"
            aria-expanded={open}
            aria-controls={panelId}
            onClick={() => setOpen((was) => !was)}
          >
            More filters
            {count > 0 ? (
              <>
                {' '}
                <span className="filter-count">{count}</span>{' '}
                <span className="visually-hidden">set</span>
              </>
            ) : null}
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

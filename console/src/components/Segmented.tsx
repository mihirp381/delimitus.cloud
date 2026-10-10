import { useId } from 'react';

interface Option<T extends string> {
  readonly value: T;
  readonly label: string;
}

interface Props<T extends string> {
  /** The group's name, read out and not shown. */
  readonly label: string;
  readonly options: readonly Option<T>[];
  readonly value: T;
  readonly onChange: (value: T) => void;
}

/**
 * A choice of one among a few, drawn as joined segments. It is a group of radio buttons, so the
 * arrow keys move the choice and a screen reader counts the options.
 */
export function Segmented<T extends string>({ label, options, value, onChange }: Props<T>) {
  const name = useId();
  return (
    <fieldset className="segmented">
      <legend className="visually-hidden">{label}</legend>
      {options.map((o) => (
        <label key={o.value} className={o.value === value ? 'segment current' : 'segment'}>
          <input
            type="radio"
            className="visually-hidden"
            name={name}
            value={o.value}
            checked={o.value === value}
            onChange={() => onChange(o.value)}
          />
          <span>{o.label}</span>
        </label>
      ))}
    </fieldset>
  );
}

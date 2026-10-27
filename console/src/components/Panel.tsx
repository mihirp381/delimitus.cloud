import { type HTMLAttributes, type ReactNode, useId } from 'react';

interface Props extends Omit<HTMLAttributes<HTMLElement>, 'title'> {
  readonly title: ReactNode;
  readonly actions?: ReactNode;
  readonly children?: ReactNode;
}

export function Panel({ title, actions, className, children, ...rest }: Props) {
  const headingId = useId();
  const heading = <h2 id={headingId}>{title}</h2>;
  return (
    <section {...rest} className={className ? `panel ${className}` : 'panel'} aria-labelledby={headingId}>
      {actions ? (
        <div className="panel-head">
          {heading}
          {actions}
        </div>
      ) : (
        heading
      )}
      {children}
    </section>
  );
}

import type { ComponentPropsWithRef } from 'react';

type Variant = 'primary' | 'secondary' | 'danger';

interface Props extends ComponentPropsWithRef<'button'> {
  readonly variant?: Variant;
}

export function Button({ variant = 'secondary', className, type = 'button', ...rest }: Props) {
  const classes = ['btn', `btn-${variant}`, className].filter(Boolean).join(' ');
  return <button type={type} className={classes} {...rest} />;
}

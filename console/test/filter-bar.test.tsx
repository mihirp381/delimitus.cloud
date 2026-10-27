import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { FilterBar } from '../src/components/FilterBar';

function bar(defaultOpen?: boolean) {
  render(
    <FilterBar
      label="Filter events"
      defaultOpen={defaultOpen}
      primary={<input aria-label="Search" />}
      more={<input aria-label="Actor" />}
    />,
  );
  return screen.getByRole('group', { name: 'Filter events' });
}

describe('the filter bar', () => {
  it('shows the primary controls and keeps the rest closed', () => {
    const group = bar();
    expect(within(group).getByRole('textbox', { name: 'Search' })).toBeTruthy();
    const toggle = within(group).getByRole('button', { name: 'More filters' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(within(group).queryByRole('textbox', { name: 'Actor' })).toBeNull();
    expect(within(group).queryByRole('region')).toBeNull();
  });

  it('opens and closes the rest from its button', () => {
    const group = bar();
    const toggle = within(group).getByRole('button', { name: 'More filters' });
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    const region = within(group).getByRole('region', { name: 'More filters' });
    expect(toggle.getAttribute('aria-controls')).toBe(region.id);
    expect(within(region).getByRole('textbox', { name: 'Actor' })).toBeTruthy();
    fireEvent.click(toggle);
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(within(group).queryByRole('region')).toBeNull();
  });

  it('can start open', () => {
    const group = bar(true);
    expect(within(group).getByRole('region', { name: 'More filters' })).toBeTruthy();
  });

  it('has no toggle without more controls', () => {
    render(<FilterBar label="Filter apps" primary={<input aria-label="Search" />} />);
    expect(screen.queryByRole('button')).toBeNull();
  });
});

import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { TOAST_MS, ToastRegion, useToast } from '../src/components/Toast';

function Say({ message }: { readonly message: string }) {
  const toast = useToast();
  return (
    <button type="button" onClick={() => toast(message)}>
      Say {message}
    </button>
  );
}

function said(): (string | null)[] {
  const region = screen.getByRole('region', { name: 'Notifications' });
  return [...region.querySelectorAll('.toast p')].map((p) => p.textContent);
}

afterEach(() => {
  vi.useRealTimers();
});

describe('the toast region', () => {
  it('is a polite live region that is there before anything is said', () => {
    render(
      <ToastRegion>
        <Say message="Saved." />
      </ToastRegion>,
    );
    const region = screen.getByRole('region', { name: 'Notifications' });
    expect(region.getAttribute('aria-live')).toBe('polite');
    expect(said()).toEqual([]);
    fireEvent.click(screen.getByRole('button', { name: 'Say Saved.' }));
    expect(said()).toEqual(['Saved.']);
  });

  it('lets a message be dismissed, and drops it by itself after a while', () => {
    vi.useFakeTimers();
    render(
      <ToastRegion>
        <Say message="One." />
        <Say message="Two." />
      </ToastRegion>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Say One.' }));
    act(() => {
      vi.advanceTimersByTime(TOAST_MS / 2);
    });
    fireEvent.click(screen.getByRole('button', { name: 'Say Two.' }));
    expect(said()).toEqual(['One.', 'Two.']);
    const region = screen.getByRole('region', { name: 'Notifications' });
    fireEvent.click(within(region).getAllByRole('button', { name: 'Dismiss' })[1]!);
    expect(said()).toEqual(['One.']);
    act(() => {
      vi.advanceTimersByTime(TOAST_MS / 2);
    });
    expect(said()).toEqual([]);
  });

  it('keeps the newest three', () => {
    render(
      <ToastRegion>
        {['One.', 'Two.', 'Three.', 'Four.'].map((m) => (
          <Say key={m} message={m} />
        ))}
      </ToastRegion>,
    );
    for (const m of ['One.', 'Two.', 'Three.', 'Four.']) {
      fireEvent.click(screen.getByRole('button', { name: `Say ${m}` }));
    }
    expect(said()).toEqual(['Two.', 'Three.', 'Four.']);
  });

  it('says nothing, and does not fail, outside the region', () => {
    render(<Say message="Lost." />);
    fireEvent.click(screen.getByRole('button', { name: 'Say Lost.' }));
    expect(screen.queryByRole('region', { name: 'Notifications' })).toBeNull();
  });
});

import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

// jsdom has no modal dialogs; the browser's own is exercised by the Playwright smoke test.
if (typeof HTMLDialogElement !== 'undefined' && !HTMLDialogElement.prototype.showModal) {
  HTMLDialogElement.prototype.showModal = function showModal(this: HTMLDialogElement) {
    this.open = true;
  };
  HTMLDialogElement.prototype.close = function close(this: HTMLDialogElement) {
    this.open = false;
    this.dispatchEvent(new Event('close'));
  };
}

// jsdom does not scroll; the router's scroll restoration calls this.
window.scrollTo = () => undefined;

afterEach(() => {
  cleanup();
  window.sessionStorage.clear();
});

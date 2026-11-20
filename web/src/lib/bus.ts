/** Window event fired after a ticket is resolved so the nav badge refreshes immediately. */
export const TICKETS_CHANGED = 'cuaflow:tickets-changed';

export function notifyTicketsChanged(): void {
  window.dispatchEvent(new Event(TICKETS_CHANGED));
}

/** 工单处理完成后派发的全局窗口事件，用于通知侧边栏徽章立即刷新计数。 */
export const TICKETS_CHANGED = 'cuaflow:tickets-changed';

export function notifyTicketsChanged(): void {
  window.dispatchEvent(new Event(TICKETS_CHANGED));
}

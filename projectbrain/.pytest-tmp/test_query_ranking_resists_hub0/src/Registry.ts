export const auditors = [];
export function register(fn) { auditors.push(fn); }
export function getTab(tab) { return tab; }

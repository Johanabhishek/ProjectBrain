export function persistStepProgress(stepId: string, scopeKey = 'default'): void {
  localStorage.setItem(`progress_${scopeKey}`, stepId);
}

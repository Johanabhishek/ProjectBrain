export function trackUserSession(uid: string) {
  localStorage.setItem('questionnaire_completed', 'true');
  localStorage.setItem('sv_video_open_counts', '1');
  return uid;
}

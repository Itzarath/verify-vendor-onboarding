/** Auto-refresh only while someone is looking.
 *
 * A tab left open for days would otherwise keep refreshing, which keeps the database awake and uses up the hosting
 * plan's monthly data. So refreshing pauses while the tab is hidden, and after ten minutes without any use of the page.
 * It resumes at the next tick once the tab is visible and used again. Work in progress (an evaluation running) keeps
 * refreshing while the page sits untouched, so a live run view never freezes, but still pauses in a hidden tab. */

const IDLE_MS = 10 * 60 * 1000;
let lastUsed = Date.now();

const markUsed = () => { lastUsed = Date.now(); };
for (const ev of ["pointerdown", "pointermove", "keydown", "wheel", "touchstart", "focus"]) {
  window.addEventListener(ev, markUsed, { passive: true });
}
document.addEventListener("visibilitychange", () => { if (!document.hidden) markUsed(); });

export function shouldRefresh(working = false): boolean {
  if (document.hidden) return false;
  return working || Date.now() - lastUsed < IDLE_MS;
}

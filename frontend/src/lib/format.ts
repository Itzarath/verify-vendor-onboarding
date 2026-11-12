export function timeAgo(iso?: string | null): string {
  if (!iso) return "—";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 45) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { day: "numeric", month: "short" });
}

export function clock(iso?: string | null): string {
  if (!iso) return "";
  return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

export function dateTime(iso?: string | null): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

export function secs(n?: number | null): string {
  if (n === null || n === undefined) return "—";
  if (n < 60) return `${n.toFixed(n < 10 ? 1 : 0)}s`;
  return `${Math.floor(n / 60)}m ${Math.round(n % 60)}s`;
}

/** "blank" means no searchable text layer (e.g. a certificate drawn as shapes); the page is still read as an image. */
export const layoutLabel = (layout?: string | null) => (layout === "blank" ? "no text layer" : layout || "");

export const humanize = (s?: string | null) => (s || "").replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());

export function mask(acct?: string | null) {
  if (!acct) return "—";
  const v = acct.replace(/\s/g, "");
  return v.length > 4 ? "••" + v.slice(-4) : v;
}

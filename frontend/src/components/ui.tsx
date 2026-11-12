import clsx from "clsx";
import { AlertTriangle, CheckCircle2, CircleDashed, Clock3, Loader2, MinusCircle, RotateCcw, XCircle, Eye } from "lucide-react";
import { useState } from "react";
import type { ReactNode } from "react";

/** A sheet: flat paper with a hairline edge. The title is a plain sentence-case heading, not a label. */
export function Card({ children, className, title, action, pad = true }: { children: ReactNode; className?: string; title?: ReactNode; action?: ReactNode; pad?: boolean }) {
  return (
    <section className={clsx("min-w-0 rounded-lg border border-slate-200 bg-white", className)}>
      {title && (
        <header className="flex flex-wrap items-center justify-between gap-3 border-b border-slate-200/80 px-5 py-3.5">
          <h2 className="text-[15px] font-semibold text-slate-900">{title}</h2>
          {action}
        </header>
      )}
      <div className={pad ? "p-5" : ""}>{children}</div>
    </section>
  );
}

export function Button({ children, onClick, variant = "primary", disabled, type = "button", size = "md", className, title }: {
  children: ReactNode; onClick?: () => void; variant?: "primary" | "secondary" | "danger" | "ghost" | "success"; disabled?: boolean;
  type?: "button" | "submit"; size?: "sm" | "md"; className?: string; title?: string;
}) {
  return (
    <button type={type} onClick={onClick} disabled={disabled} title={title}
      className={clsx("inline-flex items-center justify-center gap-1.5 rounded-md font-semibold transition-[background-color,border-color,box-shadow,transform] duration-150 active:translate-y-px disabled:cursor-not-allowed disabled:opacity-45 disabled:active:translate-y-0",
        size === "sm" ? "px-2.5 py-1.5 text-[13px]" : "px-4 py-2 text-sm",
        variant === "primary" && "bg-ink text-white shadow-[inset_0_-2px_0_rgba(0,0,0,.25)] hover:bg-brand-700",
        variant === "success" && "bg-emerald-600 text-white shadow-[inset_0_-2px_0_rgba(0,0,0,.2)] hover:bg-emerald-700",
        variant === "secondary" && "border border-slate-300 bg-white text-slate-800 hover:border-slate-400 hover:bg-slate-50",
        variant === "danger" && "border border-red-200 bg-white text-red-700 hover:border-red-300 hover:bg-red-50",
        variant === "ghost" && "text-slate-600 hover:bg-slate-100",
        className)}>
      {children}
    </button>
  );
}

const PUBLIC_STYLE: Record<string, string> = {
  pending: "bg-amber-50 text-amber-800 ring-amber-200",
  approved: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  rejected: "bg-red-50 text-red-800 ring-red-200",
};
const PUBLIC_LABEL: Record<string, string> = { pending: "Pending", approved: "Approved", rejected: "Not accepted" };

const STATE_STYLE: Record<string, string> = {
  PROCESSING: "bg-sky-50 text-sky-800 ring-sky-200",
  ACTION_REQUIRED: "bg-amber-50 text-amber-800 ring-amber-200",
  READY_FOR_DECISION: "bg-violet-50 text-violet-800 ring-violet-200",
  APPROVED: "bg-emerald-50 text-emerald-800 ring-emerald-200",
  REJECTED: "bg-red-50 text-red-800 ring-red-200",
};
export const STATE_LABEL: Record<string, string> = {
  PROCESSING: "Processing", ACTION_REQUIRED: "Action required", READY_FOR_DECISION: "Ready for decision",
  APPROVED: "Approved", REJECTED: "Rejected",
};

export function StateChip({ state, big }: { state: string; big?: boolean }) {
  return (
    <span className={clsx("inline-flex items-center gap-1.5 rounded-full font-semibold ring-1 ring-inset", STATE_STYLE[state],
      big ? "px-3 py-1 text-sm" : "px-2 py-0.5 text-xs")}>
      {state === "PROCESSING" && <span className="live-dot h-1.5 w-1.5 rounded-full bg-sky-500" />}
      {STATE_LABEL[state] || state}
    </span>
  );
}

export function PublicChip({ status, big }: { status: string; big?: boolean }) {
  return (
    <span className={clsx("inline-flex items-center rounded-full font-semibold ring-1 ring-inset", PUBLIC_STYLE[status],
      big ? "px-3 py-1 text-sm" : "px-2 py-0.5 text-xs")}>{PUBLIC_LABEL[status] || status}</span>
  );
}

const STAMP_TONE: Record<string, string> = {
  pending: "text-amber-600", approved: "text-emerald-600", rejected: "text-red-600", processing: "text-sky-600",
};
const STAMP_TEXT: Record<string, string> = {
  pending: "Pending", approved: "Approved", rejected: "Not accepted", processing: "In progress",
};
/** How a file's outcome is marked: an ink stamp. Used once per page, for the decision that matters most. */
export function Stamp({ status, size = "md", className }: { status: string; size?: "sm" | "md" | "lg"; className?: string }) {
  return (
    <span key={status} role="status" aria-label={`Status: ${STAMP_TEXT[status] || status}`}
      className={clsx("stamp stamp-press select-none", STAMP_TONE[status] || "text-slate-500",
        size === "sm" && "text-[11px]", size === "md" && "text-sm", size === "lg" && "text-xl", className)}>
      {STAMP_TEXT[status] || status}
    </span>
  );
}

/** The Verify mark: a seal with a perforated ring and a check. */
export function SealMark({ className, light }: { className?: string; light?: boolean }) {
  const fg = light ? "#15233f" : "#f3f5f4";
  const bg = light ? "#f3f5f4" : "#15233f";
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden="true">
      <circle cx="16" cy="16" r="15" fill={bg} />
      <circle cx="16" cy="16" r="11.5" fill="none" stroke={fg} strokeWidth="1.2" strokeDasharray="2 1.6" opacity=".7" />
      <path d="M10.5 16.5l3.6 3.6 7.4-8" stroke={fg} strokeWidth="2.6" fill="none" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

const OWNER_STYLE: Record<string, string> = {
  vendor: "bg-amber-100 text-amber-900", reviewer: "bg-violet-100 text-violet-900", finance: "bg-teal-100 text-teal-900",
  system: "bg-slate-200 text-slate-700",
};
const OWNER_LABEL: Record<string, string> = { vendor: "Vendor", reviewer: "Reviewer", finance: "Finance", system: "System" };
export function OwnerBadge({ owner }: { owner?: string | null }) {
  if (!owner) return null;
  return <span className={clsx("rounded px-1.5 py-0.5 text-[11px] font-semibold", OWNER_STYLE[owner])}>{OWNER_LABEL[owner] || owner}</span>;
}

export function CheckIcon({ status, className }: { status: string; className?: string }) {
  const c = clsx("h-4 w-4 shrink-0", className);
  switch (status) {
    case "satisfied": return <CheckCircle2 className={clsx(c, "text-emerald-600")} />;
    case "not_satisfied": return <XCircle className={clsx(c, "text-red-600")} />;
    case "needs_review": return <Eye className={clsx(c, "text-violet-600")} />;
    case "unable": return <AlertTriangle className={clsx(c, "text-amber-600")} />;
    case "not_applicable": return <MinusCircle className={clsx(c, "text-slate-400")} />;
    case "info": return <CircleDashed className={clsx(c, "text-slate-400")} />;
    default: return <Clock3 className={clsx(c, "text-slate-400")} />;
  }
}

export const CHECK_STATUS_LABEL: Record<string, string> = {
  satisfied: "Satisfied", not_satisfied: "Not satisfied", needs_review: "Needs review", unable: "Unable to confirm",
  not_applicable: "Not applicable", info: "Note",
};

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={clsx("h-4 w-4 animate-spin", className)} />;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="rounded-md border border-dashed border-slate-300 px-4 py-6 text-center text-sm text-slate-500">{children}</div>;
}

export function ErrorNote({ error }: { error: any }) {
  if (!error) return null;
  return (
    <div role="alert" className="flex items-start gap-2 rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">
      <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
      <span>{error.message || String(error)}</span>
    </div>
  );
}

/** A load that failed: say what didn't load and offer a retry that doesn't need a browser reload. */
export function LoadFailed({ what, error, onRetry, children }: { what: string; error: any; onRetry: () => void; children?: ReactNode }) {
  const [busy, setBusy] = useState(false);
  return (
    <div className="space-y-3 rounded-lg border border-red-200 bg-white p-4">
      <p className="text-sm font-semibold text-slate-900">Couldn't load {what}</p>
      <ErrorNote error={error} />
      <div className="flex flex-wrap items-center gap-3">
        <Button variant="secondary" disabled={busy} onClick={async () => { setBusy(true); try { await onRetry(); } finally { setBusy(false); } }}>
          <RotateCcw className="h-4 w-4" />Try again</Button>
        {children}
      </div>
    </div>
  );
}

export function Field({ label, children, hint, required }: { label: string; children: ReactNode; hint?: ReactNode; required?: boolean }) {
  return (
    <label className="block">
      <span className="mb-1 block text-[13px] font-semibold text-slate-700">{label}{required && <span className="text-red-600" aria-hidden="true"> *</span>}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
    </label>
  );
}

export const inputCls = "w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-900 shadow-[inset_0_1px_1px_rgba(21,35,63,.04)] placeholder:text-slate-400 transition-colors hover:border-slate-400 focus:border-brand-500 focus:ring-2 focus:ring-brand-100 disabled:bg-slate-50 disabled:text-slate-500";

export function Provenance({ p }: { p?: string | null }) {
  if (!p || p === "none") return null;
  const map: Record<string, [string, string]> = {
    text_layer_verified: ["text-verified", "bg-emerald-50 text-emerald-700"],
    visual_unconfirmed: ["read from image", "bg-amber-50 text-amber-800"],
    quote_not_in_text: ["not in text layer", "bg-red-50 text-red-700"],
    inferred: ["inferred", "bg-slate-100 text-slate-600"],
  };
  const [label, cls] = map[p] || [p, "bg-slate-100 text-slate-600"];
  return <span className={clsx("rounded px-1.5 py-0.5 text-[11px] font-medium", cls)} title={p}>{label}</span>;
}

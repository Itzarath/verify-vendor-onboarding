import clsx from "clsx";
import { AlertTriangle, Check, FileSearch, GitCompareArrows, Inbox, ListChecks, Scale, Send, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { shouldRefresh } from "../lib/activity";
import { api } from "../lib/api";
import { clock, secs } from "../lib/format";
import { useNow } from "../lib/hooks";

const STAGES = [
  { key: "receive", label: "Receive", icon: Inbox, help: "What arrived, and which case it belongs to" },
  { key: "read", label: "Read", icon: FileSearch, help: "What each document actually says" },
  { key: "compare", label: "Compare", icon: GitCompareArrows, help: "Form vs documents vs previous version" },
  { key: "check", label: "Check", icon: ListChecks, help: "Buyer policy, ID rules, registry" },
  { key: "decide", label: "Decide", icon: Scale, help: "Approve-ready, needs action, or reject" },
  { key: "communicate", label: "Communicate", icon: Send, help: "Tasks, vendor message, history" },
];

type Ev = { id: number; stage: string; status: string; title: string; detail?: any; at: string };

/** Events about one document report that document, not the whole stage. Staff see its id; vendors, whose events
 * carry no details, see only `scope: "document"`. */
const perDocument = (e: Ev) => e.detail?.document_id !== undefined || e.detail?.scope === "document";

function stageState(events: Ev[], key: string, runStatus: string, currentStage: string | null) {
  const evs = events.filter((e) => e.stage === key);
  if (evs.some((e) => e.status === "failed")) return "failed";
  // Only the stage's own summary completes it: in Read, the first finished document must not turn the stage green
  // while the others are still being read.
  const done = evs.some((e) => e.status === "completed" && !perDocument(e)) || (key === "decide" && evs.some((e) => e.status === "warning"));
  const warned = evs.some((e) => e.status === "warning");
  if (done) return warned ? "warning" : "done";
  if (currentStage === key && (runStatus === "RUNNING" || runStatus === "QUEUED")) return "active";
  if (evs.length) return warned ? "warning" : "active";
  return "pending";
}

export default function RunView({ runId, onFinished, compact }: { runId: number; onFinished?: () => void; compact?: boolean }) {
  const [events, setEvents] = useState<Ev[]>([]);
  const [run, setRun] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);
  const lastId = useRef(0);
  const finishedFor = useRef<number | null>(null);

  useEffect(() => {
    setEvents([]); setRun(null); setError(null); lastId.current = 0; finishedFor.current = null;
    let stop = false;
    let timer: any;
    let working = true; // until the first reply says otherwise
    const tick = async () => {
      // Only while someone is looking (lib/activity); a running evaluation keeps refreshing on an untouched page.
      if (!shouldRefresh(working)) { timer = setTimeout(tick, 2000); return; }
      try {
        const d = await api(`/runs/${runId}/events?after=${lastId.current}`);
        if (stop) return;
        if (d.events.length) {
          lastId.current = d.events[d.events.length - 1].id;
          setEvents((prev) => [...prev, ...d.events]);
        }
        setRun(d.run);
        setError(null); // the connection is back: an earlier polling error no longer applies
        const active = d.run.status === "QUEUED" || d.run.status === "RUNNING";
        working = active;
        if (!active && finishedFor.current !== runId) {
          finishedFor.current = runId;
          onFinished?.();
        }
        timer = setTimeout(tick, active ? 900 : 8000);
      } catch (e: any) {
        if (!stop) { setError(e.message); timer = setTimeout(tick, 3000); }
      }
    };
    tick();
    return () => { stop = true; clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  const active = run && (run.status === "QUEUED" || run.status === "RUNNING");
  const now = useNow(!!active, 500);
  const currentStage = events.length ? events[events.length - 1].stage : null;
  const elapsed = run?.started_at ? ((run.finished_at ? new Date(run.finished_at).getTime() : now) - new Date(run.started_at).getTime()) / 1000 : null;
  const [open, setOpen] = useState<string | null>(null);
  const grouped = useMemo(() => STAGES.map((s) => ({ ...s, events: events.filter((e) => e.stage === s.key),
    state: stageState(events, s.key, run?.status || "QUEUED", currentStage) })), [events, run, currentStage]);

  // How far the slip has travelled: the last stage that has finished (done, with warnings, or failed).
  const reached = grouped.reduce((n, s, i) => (["done", "warning", "failed"].includes(s.state) ? i : n), -1);

  return (
    <div>
      <div className="mb-5 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2 text-sm">
          <span className="display font-semibold text-slate-900">Run {runId}</span>
          {run && <RunStatus status={run.status} />}
          {run?.trigger && <span className="text-slate-500">{run.trigger.replace(/_/g, " ")}</span>}
          {run?.attempt > 1 && <span className="font-medium text-amber-700">attempt {run.attempt}</span>}
        </div>
        <div className="text-sm tabular-nums text-slate-500">{elapsed !== null ? `${secs(elapsed)} elapsed` : run?.ahead
          ? `Queued: ${run.ahead} earlier evaluation${run.ahead > 1 ? "s" : ""} first (they run one at a time)` : "Waiting for the worker…"}</div>
      </div>
      {/* The routing slip: six stages on one track; the track fills as stages finish. Each stage filters the log. */}
      <ol className="relative grid grid-cols-3 gap-y-5 sm:grid-cols-6">
        <span aria-hidden="true" className="absolute left-[8.33%] right-[8.33%] top-4 hidden h-0.5 rounded bg-slate-200 sm:block" />
        <span aria-hidden="true" className="absolute left-[8.33%] top-4 hidden h-0.5 rounded bg-ink transition-[width] duration-700 ease-out sm:block"
          style={{ width: reached > 0 ? `calc((100% - 16.66%) * ${reached / 5})` : 0 }} />
        {grouped.map((s) => {
          const Icon = s.icon;
          return (
            <li key={s.key} className="relative">
              <button onClick={() => setOpen(open === s.key ? null : s.key)} title={s.help} aria-pressed={open === s.key}
                className="group flex w-full flex-col items-center gap-1.5 rounded-md px-1 pb-1 text-center">
                <span className={clsx("relative z-10 flex h-8 w-8 items-center justify-center rounded-full transition-colors duration-300",
                  s.state === "done" && "bg-emerald-600 text-white", s.state === "warning" && "bg-amber-500 text-white",
                  s.state === "failed" && "bg-red-600 text-white",
                  s.state === "active" && "bg-white text-sky-600 ring-2 ring-sky-500",
                  s.state === "pending" && "bg-white text-slate-400 ring-2 ring-slate-200",
                  open === s.key && "outline-2 outline-offset-2 outline-ink")}>
                  {s.state === "done" ? <Check className="h-4 w-4" /> : s.state === "failed" ? <X className="h-4 w-4" /> :
                    s.state === "warning" ? <AlertTriangle className="h-4 w-4" /> : <Icon className={clsx("h-4 w-4", s.state === "active" && "live-dot")} />}
                </span>
                <span className={clsx("text-sm font-semibold", s.state === "pending" ? "text-slate-400" : "text-slate-900")}>{s.label}</span>
                <span className="text-xs text-slate-500">{s.events.length ? `${s.events.length} event${s.events.length > 1 ? "s" : ""}` : s.state === "active" ? "working…" : " "}</span>
              </button>
            </li>
          );
        })}
      </ol>
      {error && <p className="mt-2 text-xs text-red-600">{error}</p>}
      <div className={clsx("mt-5 overflow-y-auto rounded-md border border-slate-200 bg-slate-50", compact ? "max-h-56" : "max-h-80")}>
        <ul className="divide-y divide-slate-200/70">
          {(open ? events.filter((e) => e.stage === open) : events).map((e) => (
            <li key={e.id} className="event-in flex items-start gap-3 px-3 py-2 text-[13px]">
              <span className="mono w-20 shrink-0 whitespace-nowrap pt-0.5 text-[11px] text-slate-400">{clock(e.at)}</span>
              <span className="hidden w-24 shrink-0 pt-0.5 text-xs font-semibold capitalize text-slate-400 sm:block">{e.stage}</span>
              <EventDot status={e.status} />
              <span className={clsx("min-w-0 flex-1 break-words", e.status === "warning" && "text-amber-900", e.status === "failed" && "text-red-800")}>{e.title}</span>
            </li>
          ))}
          {active && (
            <li className="flex items-center gap-3 px-3 py-2 text-[13px] text-slate-500">
              <span className="w-20" /><span className="hidden w-24 sm:block" />
              <span className="live-dot mt-0.5 h-2 w-2 rounded-full bg-sky-500" />
              <span>{currentStage === "read" ? "Waiting for the document model…" : "Working…"}</span>
            </li>
          )}
        </ul>
      </div>
      {open && <button onClick={() => setOpen(null)} className="mt-2 text-sm font-medium text-brand-600 hover:underline">Show every stage</button>}
    </div>
  );
}

function EventDot({ status }: { status: string }) {
  return <span className={clsx("mt-1.5 h-2 w-2 shrink-0 rounded-full",
    status === "completed" && "bg-emerald-500", status === "warning" && "bg-amber-500", status === "failed" && "bg-red-500",
    status === "started" && "bg-sky-500", status === "info" && "bg-slate-300")} />;
}

export function RunStatus({ status }: { status: string }) {
  const s: Record<string, string> = {
    QUEUED: "bg-slate-100 text-slate-700", RUNNING: "bg-sky-100 text-sky-800", SUCCEEDED: "bg-emerald-100 text-emerald-800",
    FAILED: "bg-red-100 text-red-800", SUPERSEDED: "bg-slate-100 text-slate-500 line-through",
  };
  const label: Record<string, string> = { QUEUED: "Queued", RUNNING: "Running", SUCCEEDED: "Finished", FAILED: "Failed", SUPERSEDED: "Superseded" };
  return <span className={clsx("rounded px-1.5 py-0.5 text-xs font-semibold", s[status])}>{label[status] || status}</span>;
}

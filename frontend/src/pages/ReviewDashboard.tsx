import clsx from "clsx";
import { ChevronRight, Clock3, PlugZap, Timer } from "lucide-react";
import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { RunStatus } from "../components/RunView";
import { Card, Empty, LoadFailed, OwnerBadge, Spinner, StateChip } from "../components/ui";
import { api } from "../lib/api";
import { secs, timeAgo } from "../lib/format";
import { useResource } from "../lib/hooks";

const FILTERS = [
  { key: "attention", label: "Needs my attention" },
  { key: "all", label: "All" },
  { key: "PROCESSING", label: "Processing" },
  { key: "ACTION_REQUIRED", label: "Action required" },
  { key: "READY_FOR_DECISION", label: "Ready" },
  { key: "APPROVED", label: "Approved" },
  { key: "REJECTED", label: "Rejected" },
];

export default function ReviewDashboard() {
  const { data, reload, error } = useResource<any>("/review/dashboard", 4000);
  const [filter, setFilter] = useState("attention");
  const nav = useNavigate();
  if (!data && error) return <LoadFailed what="the dashboard" error={error} onRetry={reload} />;
  if (!data) return <div className="flex justify-center py-20"><Spinner className="h-6 w-6 text-slate-400" /></div>;
  const c = data.counts;
  const rows = data.requests.filter((r: any) =>
    filter === "all" ? true : filter === "attention"
      ? r.state === "READY_FOR_DECISION" || (r.state === "ACTION_REQUIRED" && r.waiting_on.some((o: string) => o !== "vendor"))
      : r.state === filter);

  const outage = async () => { await api("/review/demo/registry-outage", { body: { on: !data.registry_outage } }); reload(); };

  const segments: [string, string, number, string][] = [
    ["PROCESSING", "Processing", c.PROCESSING, "bg-sky-500"],
    ["ACTION_REQUIRED", "Action required", c.ACTION_REQUIRED, "bg-amber-500"],
    ["READY_FOR_DECISION", "Ready for decision", c.READY_FOR_DECISION, "bg-violet-500"],
    ["APPROVED", "Approved", c.APPROVED, "bg-emerald-600"],
    ["REJECTED", "Rejected", c.REJECTED, "bg-red-600"],
  ];

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-[1.9rem] font-bold leading-tight text-slate-900">Applications</h1>
          <p className="mt-0.5 text-[15px] text-slate-600">{data.buyer.name}, under {data.buyer.policy}</p>
        </div>
        {data.needs_my_attention > 0 && (
          <button onClick={() => setFilter("attention")} className="rounded-md bg-amber-100 px-3 py-1.5 text-sm font-semibold text-amber-900 transition-colors hover:bg-amber-200">
            {data.needs_my_attention} need{data.needs_my_attention === 1 ? "s" : ""} your attention
          </button>
        )}
      </div>

      {/* The pipeline: where every application stands, in proportion. Each state filters the queue. */}
      <section className="rounded-lg border border-slate-200 bg-white p-5">
        <div className="flex items-baseline justify-between gap-3">
          <h2 className="text-[15px] font-semibold text-slate-900">Pipeline</h2>
          <span className="text-sm text-slate-500"><b className="display text-lg text-slate-900">{data.total}</b> application{data.total === 1 ? "" : "s"}</span>
        </div>
        <div className="mt-3 flex h-3 overflow-hidden rounded-full bg-slate-100" aria-hidden="true">
          {segments.filter(([, , n]) => n > 0).map(([k, , n, bg]) => (
            <div key={k} className={clsx(bg, "h-full border-r-2 border-white transition-[width] duration-500 last:border-r-0")} style={{ width: `${(n / Math.max(1, data.total)) * 100}%` }} />
          ))}
        </div>
        <div className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-5">
          {segments.map(([k, label, n, bg]) => (
            <button key={k} onClick={() => setFilter(filter === k ? "all" : k)} aria-pressed={filter === k}
              className={clsx("flex items-center gap-2.5 rounded-md border px-3 py-2 text-left transition-colors",
                filter === k ? "border-slate-900 bg-slate-50" : "border-slate-200 hover:border-slate-300 hover:bg-slate-50")}>
              <span className={clsx("h-2.5 w-2.5 shrink-0 rounded-full", bg)} />
              <span className="min-w-0 flex-1 text-sm leading-tight text-slate-600">{label}</span>
              <span className="display text-lg font-bold tabular-nums text-slate-900">{n}</span>
            </button>
          ))}
        </div>
      </section>

      <Card pad={false} title="Queue" action={
        <div className="flex flex-wrap gap-1">
          {FILTERS.map((f) => (
            <button key={f.key} onClick={() => setFilter(f.key)} className={clsx("rounded-md px-2.5 py-1 text-[13px] font-semibold transition-colors",
              filter === f.key ? "bg-ink text-white" : "text-slate-600 hover:bg-slate-100")}>
              {f.label}{f.key === "attention" && data.needs_my_attention ? ` (${data.needs_my_attention})` : ""}
            </button>
          ))}
        </div>}>
        {rows.length === 0 ? <div className="p-5"><Empty>Nothing here right now.</Empty></div> : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead><tr className="border-b border-slate-200 text-left text-[13px] text-slate-500">
                <th className="px-5 py-2.5 font-medium">Vendor</th><th className="px-3 py-2.5 font-medium">Stage</th>
                <th className="px-3 py-2.5 font-medium">Waiting on</th><th className="px-3 py-2.5 font-medium">Risk</th><th className="px-3 py-2.5 font-medium">Revision</th>
                <th className="px-3 py-2.5 font-medium">Last run</th><th className="px-3 py-2.5 font-medium">Updated</th><th />
              </tr></thead>
              <tbody className="divide-y divide-slate-200/70">
                {rows.map((r: any) => (
                  <tr key={r.id} onClick={() => nav(`/review/requests/${r.id}`)} className="cursor-pointer hover:bg-slate-50">
                    <td className="px-5 py-3">
                      <div className="flex items-center gap-2">
                        {r.high_severity && <span className="h-2 w-2 rounded-full bg-red-500" title="Identity or bank-ownership concern" />}
                        {/* A real link, so the row can be opened from the keyboard; a mouse can still click anywhere on the row. */}
                        <Link to={`/review/requests/${r.id}`} onClick={(e) => e.stopPropagation()}
                          className="font-semibold text-slate-900 hover:underline">{r.vendor_name}</Link>
                      </div>
                      <span className="mono text-xs text-slate-500">{r.reference}</span>{r.kind === "change" && <span className="ml-1.5 text-xs text-sky-700">change request</span>}
                    </td>
                    <td className="px-3 py-3"><StateChip state={r.state} /></td>
                    <td className="px-3 py-3"><div className="flex flex-wrap gap-1">{r.waiting_on.length ? r.waiting_on.map((o: string) => <OwnerBadge key={o} owner={o} />) :
                      r.state === "READY_FOR_DECISION" ? <OwnerBadge owner="reviewer" /> : <span className="text-xs text-slate-400">—</span>}</div></td>
                    <td className="px-3 py-3">{r.risk ? <span title={`${r.risk.score}/100`} className={clsx("rounded px-1.5 py-0.5 text-[11px] font-bold uppercase",
                      r.risk.tier === "high" ? "bg-red-100 text-red-900" : r.risk.tier === "medium" ? "bg-amber-100 text-amber-900" : "bg-emerald-100 text-emerald-900")}>{r.risk.tier}</span>
                      : <span className="text-xs text-slate-400">—</span>}</td>
                    <td className="px-3 py-3 text-slate-600">{r.revision}</td>
                    <td className="px-3 py-3">{r.last_run && <div className="flex items-center gap-1.5"><RunStatus status={r.last_run.status} /><span className="text-xs text-slate-500">{secs(r.last_run.duration_s)}</span></div>}</td>
                    <td className="px-3 py-3 text-xs text-slate-500">{timeAgo(r.updated_at)}</td>
                    <td className="px-3 py-3 text-right"><ChevronRight className="inline h-4 w-4 text-slate-300" /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>

      <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1fr)_320px]">
        <Card pad={false} title="Run history (all cases)" action={<span className="flex items-center gap-1 text-xs text-slate-500"><Timer className="h-3.5 w-3.5" />avg {secs(data.avg_processing_s)}</span>}>
          <div className="max-h-96 overflow-y-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-white"><tr className="border-b border-slate-200 text-left text-[13px] text-slate-500">
                <th className="px-5 py-2 font-medium">Run</th><th className="px-3 py-2 font-medium">Case</th><th className="px-3 py-2 font-medium">Trigger</th>
                <th className="px-3 py-2 font-medium">Status</th><th className="px-3 py-2 font-medium">Outcome</th><th className="px-3 py-2 font-medium">Duration</th><th className="px-3 py-2 font-medium">When</th>
              </tr></thead>
              <tbody className="divide-y divide-slate-200/70">
                {data.runs.map((r: any) => (
                  <tr key={r.id} className="hover:bg-slate-50">
                    <td className="mono px-5 py-2 text-xs text-slate-600">#{r.id}</td>
                    <td className="px-3 py-2"><Link to={`/review/requests/${r.request_id}`} className="font-medium text-slate-800 hover:underline">{r.vendor_name}</Link> <span className="text-xs text-slate-500">rev {r.revision}</span></td>
                    <td className="px-3 py-2 text-xs text-slate-600">{r.trigger.replace(/_/g, " ")}</td>
                    <td className="px-3 py-2"><RunStatus status={r.status} /></td>
                    <td className="px-3 py-2 text-xs text-slate-700">{r.outcome ? r.outcome.replace(/_/g, " ") : "—"}</td>
                    <td className="px-3 py-2 text-xs text-slate-600">{secs(r.duration_s)}</td>
                    <td className="px-3 py-2 text-xs text-slate-500">{timeAgo(r.queued_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
        {data.registry_simulated && <Card title="Demo controls">
          <p className="mb-3 text-sm text-slate-600">Simulate the external GST registry being down, to show that an outage keeps a case pending instead of approving or rejecting it.</p>
          <button onClick={outage} className={clsx("flex w-full items-center justify-between rounded-lg border px-3 py-2.5 text-sm font-medium",
            data.registry_outage ? "border-red-200 bg-red-50 text-red-800" : "border-slate-200 bg-white text-slate-700 hover:bg-slate-50")}>
            <span className="flex items-center gap-2"><PlugZap className="h-4 w-4" />Registry outage</span>
            <span>{data.registry_outage ? "ON" : "off"}</span>
          </button>
          <p className="mt-3 flex items-start gap-1.5 text-xs text-slate-500"><Clock3 className="mt-0.5 h-3.5 w-3.5 shrink-0" />Applies to new lookups only; resets when the server restarts.</p>
        </Card>}
      </div>
    </div>
  );
}


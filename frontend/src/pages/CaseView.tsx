import clsx from "clsx";
import { ArrowLeft, BadgeCheck, Check, FileText, Gavel, History, Landmark, ListChecks, RefreshCw, RotateCcw, Search, ShieldAlert, X } from "lucide-react";
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAuth } from "../App";
import RunView, { RunStatus } from "../components/RunView";
import { Button, Card, CHECK_STATUS_LABEL, CheckIcon, Empty, ErrorNote, inputCls, LoadFailed, OwnerBadge, Provenance, Spinner, Stamp, StateChip } from "../components/ui";
import { api, opId } from "../lib/api";
import { dateTime, humanize, layoutLabel, mask, secs, timeAgo } from "../lib/format";
import { useResource } from "../lib/hooks";

const GROUPS: [string, string][] = [
  ["completeness", "Completeness"], ["format", "ID format rules"], ["consistency", "Consistency"], ["credibility", "Credibility"],
  ["authenticity", "Certificate signatures"], ["verification", "Independent verification"], ["declaration", "Declarations"],
  ["change", "Changes since last revision"], ["scope", "Scope"],
];

export default function CaseView() {
  const { id } = useParams();
  // A fresh page per case: the run selection, open forms and typed notes never carry over to another case.
  return <CasePage key={id} id={id!} />;
}

function CasePage({ id }: { id: string }) {
  const [poll, setPoll] = useState<number | null>(null);
  const { data, reload, error } = useResource<any>(`/review/requests/${id}`, poll);
  const [tab, setTab] = useState("findings");
  const [runId, setRunId] = useState<number | null>(null);
  const [newer, setNewer] = useState<number | null>(null);
  const state = data?.request.state;
  useEffect(() => setPoll(state === "PROCESSING" ? 2500 : null), [state]);
  useEffect(() => { if (data) { setRunId(data.runs[0]?.id ?? null); setNewer(null); } }, [data?.request.id, data?.request.version]); // eslint-disable-line
  // Watch for changes made elsewhere WITHOUT replacing what the reviewer is looking at.
  useEffect(() => {
    if (!data || state === "PROCESSING") return;
    const t = setInterval(async () => {
      try { const d = await api(`/review/requests/${id}`); if (d.request.version !== data.request.version) setNewer(d.request.version); } catch { /* ignore */ }
    }, 5000);
    return () => clearInterval(t);
  }, [data, id, state]);

  if (error && !data) return (
    <div className="space-y-5">
      <Link to="/review" className="inline-flex items-center gap-1 text-sm font-medium text-slate-500 hover:text-slate-900"><ArrowLeft className="h-4 w-4" />Dashboard</Link>
      <LoadFailed what="this application" error={error} onRetry={reload} />
    </div>
  );
  // Never show (or act on) one case's data under another case's address.
  if (!data || String(data.request.id) !== id) return <div className="flex justify-center py-20"><Spinner className="h-6 w-6 text-slate-400" /></div>;
  const { request: r, vendor, policy, assessment: a } = data;
  const rev = data.revisions.find((x: any) => x.id === data.current_revision_id);
  // the evidence tab always pairs an assessment with ITS revision (they differ while a correction is being processed)
  const evRev = data.revisions.find((x: any) => x.id === data.assessment?.revision_id) ?? rev;
  const openTasks = data.tasks.filter((t: any) => t.status === "open");

  return (
    <div className="space-y-5">
      <Link to="/review" className="inline-flex items-center gap-1 text-sm font-medium text-slate-500 hover:text-slate-900"><ArrowLeft className="h-4 w-4" />Dashboard</Link>
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-4 rounded-lg border border-slate-200 bg-white p-6">
        <div className="min-w-0">
          <div className="mb-2 flex flex-wrap items-center gap-2"><StateChip state={r.state} />
            {r.kind === "change" && <span className="rounded bg-sky-50 px-2 py-0.5 text-xs font-semibold text-sky-800 ring-1 ring-sky-200">Change request</span>}</div>
          <h1 className="text-[1.9rem] font-bold leading-tight text-slate-900">{vendor.name}</h1>
          <p className="mt-1 text-sm text-slate-600"><span className="mono text-slate-700">{r.reference}</span>, revision {r.revision} of {data.revisions.length}, under {policy.label}
            {policy.is_test_policy && <span className="ml-2 inline-block whitespace-nowrap rounded bg-amber-50 px-1.5 py-0.5 text-[11px] text-amber-800 ring-1 ring-amber-200">Test policy, synthetic data</span>}
            {(policy.verification || []).map((v: any) => (
              <span key={v.identifier} className="ml-2 inline-block whitespace-nowrap rounded bg-slate-50 px-1.5 py-0.5 text-[11px] text-slate-700 ring-1 ring-slate-200">
                {v.identifier} registry: {v.accepted_methods.includes("simulated_registry")
                  ? (v.accepted_methods.includes("manual_registry") ? "simulated or manual lookup" : "simulated only")
                  : "manual lookup only"}
              </span>))}</p>
        </div>
        <div className="flex flex-col items-start gap-3 sm:items-end">
          <Stamp status={r.state === "PROCESSING" ? "processing" : r.public_status} />
          <div className="text-xs text-slate-500 sm:text-right">Case version <span className="mono text-slate-700">v{r.version}</span>, updated {timeAgo(r.updated_at)}</div>
        </div>
      </div>
      {a && a.revision_id !== data.current_revision_id && (
        <div className="rounded-xl border border-sky-200 bg-sky-50 px-4 py-3 text-sm text-sky-900">
          Findings below are from the assessment of <b>revision {data.revisions.find((x: any) => x.id === a.revision_id)?.number}</b>.
          Revision {r.revision} {r.state === "PROCESSING" ? "is being processed now" : "has no completed assessment yet"}; its results will replace these.
        </div>
      )}
      {newer && (
        <div className="flex items-center justify-between gap-3 rounded-xl border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          <span><b>This case changed while you were looking at it</b> (v{r.version} → v{newer}). Decisions you take on this page will be refused until you reload.</span>
          <Button variant="secondary" size="sm" onClick={reload}><RefreshCw className="h-3.5 w-3.5" />Reload</Button>
        </div>
      )}

      <div className="grid grid-cols-1 gap-5 xl:grid-cols-[minmax(0,1fr)_380px]">
        <div className="min-w-0 space-y-5">
          <Card title="Live run view" action={data.runs.length > 1 && (
            <select className="rounded-md border border-slate-200 px-2 py-1 text-xs" value={runId ?? ""} onChange={(e) => setRunId(Number(e.target.value))}>
              {data.runs.map((x: any) => <option key={x.id} value={x.id}>Run #{x.id} · {x.trigger.replace(/_/g, " ")} · {x.status.toLowerCase()}</option>)}
            </select>)}>
            {runId ? <RunView runId={runId} onFinished={() => r.state === "PROCESSING" && reload()} /> : <Empty>No runs yet.</Empty>}
          </Card>

          <Card pad={false}>
            <div className="flex flex-wrap gap-x-1 border-b border-slate-100 px-3 pt-2" role="tablist">
              {[["findings", "Findings", ListChecks], ["evidence", "Evidence", Search], ["changes", "Changes", RotateCcw], ["history", "History", History]].map(([k, l, I]: any) => (
                <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)} className={clsx("flex items-center gap-1.5 border-b-2 px-3 py-2.5 text-sm font-medium",
                  tab === k ? "border-ink text-slate-900" : "border-transparent text-slate-500 hover:text-slate-800")}><I className="h-4 w-4" />{l}</button>
              ))}
            </div>
            <div className="p-5">
              {tab === "findings" && <Findings a={a} />}
              {tab === "evidence" && <Evidence key={evRev?.id} a={a} rev={evRev} />}
              {tab === "changes" && <Changes a={a} revisions={data.revisions} />}
              {tab === "history" && <HistoryTab data={data} />}
            </div>
          </Card>
        </div>

        <div className="min-w-0 space-y-5">
          {/* Keyed by the revision and its assessment: a half-written approval or rejection is about the evidence it
              was written against, so a reload that brings a new revision closes it instead of applying it to the new one. */}
          <DecisionPanel key={`${data.current_revision_id}:${data.assessment?.id ?? ""}`} data={data} onDone={reload} />
          <RiskPanel data={data} />
          <TasksPanel data={data} tasks={openTasks} onDone={reload} />
          <RegistryPanel data={data} onDone={reload} />
          <VendorMasterPanel data={data} stale={newer !== null} />
        </div>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ risk and the vendor master record
const RISK_STYLE: Record<string, string> = { low: "bg-emerald-100 text-emerald-900", medium: "bg-amber-100 text-amber-900", high: "bg-red-100 text-red-900" };

function RiskPanel({ data }: { data: any }) {
  const risk = data.assessment?.summary?.risk;
  if (!risk) return null;
  const rules = data.approval_rules || {};
  return (
    <Card title={<span className="flex items-center gap-2"><ShieldAlert className="h-3.5 w-3.5" />Supplier risk</span>}>
      <div className="flex items-center gap-3">
        <span className={clsx("rounded px-2 py-0.5 text-xs font-bold uppercase tracking-wide", RISK_STYLE[risk.tier])}>{risk.tier}</span>
        <span className="text-sm text-slate-700"><b className="tabular-nums">{risk.score}</b>/100</span>
      </div>
      {risk.flags.length ? (
        <ul className="mt-3 space-y-1.5 text-[13px] text-slate-700">
          {risk.flags.map((f: any, i: number) => <li key={i} className="flex gap-2"><span className="mono w-8 shrink-0 text-right text-slate-500">+{f.weight}</span><span>{f.flag}</span></li>)}
        </ul>
      ) : <p className="mt-2 text-[13px] text-slate-600">No red flags in the findings.</p>}
      <p className="mt-3 text-[11px] text-slate-500">From the findings, weighted like production KYB scores. This buyer's policy approves automatically only at {rules.auto_approve_max_risk ?? "low"} risk{rules.second_approver_from_risk && rules.second_approver_from_risk !== "never" ? `, and needs two approvers from ${rules.second_approver_from_risk} risk` : ""}.</p>
    </Card>
  );
}

function VendorMasterPanel({ data, stale }: { data: any; stale: boolean }) {
  const m = data.vendor_master;
  if (!m) return null;
  // The file is bound to the version on screen: the server refuses it if the case has changed since.
  const base = `/api/review/requests/${data.request.id}/vendor-master?version=${data.request.version}`;
  const rows: [string, ReactNode][] = [
    ["Vendor code", <span className="mono">{m.vendor_code}</span>],
    ["GST", m.gst?.registered ? <>{m.gst.gstin} · {m.gst.taxpayer_type || "type not recorded"}{m.gst.taxpayer_type_source ? <span className="text-slate-500"> ({m.gst.taxpayer_type_source})</span> : null}{m.gst.input_tax_credit ? <> · ITC {m.gst.input_tax_credit.split(/[:;(]/)[0].trim().toLowerCase()}</> : null}</> : "Not registered"],
    ["MSME", m.msme?.registered ? <>{m.msme.category || "category not read"}{m.msme.max_days_with_written_agreement ? <> · at most {m.msme.max_days_with_written_agreement} days with a written agreement, {m.msme.max_days_without_agreement} without (MSMED s.15)<div className="text-[11px] text-slate-500">Agreed terms are not recorded at onboarding; set them from the signed agreement.</div></> : null}</> : "Not MSME"],
    ["TDS", m.tds?.table_entry ? <>{m.tds.law} {m.tds.table_entry}: {m.tds.nature}, {m.tds.rate} <span className="text-slate-400">(was {m.tds.old_reference})</span>
      <div className="text-[11px] text-slate-500">Suggested from the service category; the tax team confirms.{m.tds.note ? ` ${m.tds.note}.` : ""}</div></> : m.tds?.status],
    ["Bank", <>{m.bank?.bank_name} · ••{String(m.bank?.account_number || "").slice(-4)} · {m.bank?.ifsc}{m.bank?.verified_at_bank ? <> · bank check: {String(m.bank.verified_at_bank.result).toLowerCase().replace(/_/g, " ")}{String(m.bank.verified_at_bank.method || "").includes("sandbox") ? <span className="ml-1 rounded bg-amber-100 px-1 text-[11px] font-bold text-amber-900">SANDBOX</span> : null}</> : null}</>],
    ["Payments", m.payment_setup ? <>{m.payment_setup.status === "hold" ? <span className="rounded bg-amber-100 px-1 text-[11px] font-bold text-amber-900">ON HOLD</span> : <span className="rounded bg-emerald-100 px-1 text-[11px] font-bold text-emerald-900">READY</span>}
      {m.payment_setup.hold_reasons ? <span className="text-slate-600">: {m.payment_setup.hold_reasons}</span> : null}
      {m.payment_setup.bank_change_confirmation ? <div className="text-[11px] text-slate-500">Bank change confirmed by {m.payment_setup.bank_change_confirmation.by}: {m.payment_setup.bank_change_confirmation.method_label} ({m.payment_setup.bank_change_confirmation.contact_source_label}).</div> : null}</> : "—"],
    ["Risk", m.risk?.tier ? `${m.risk.tier} (${m.risk.score}/100)` : "—"],
  ];
  return (
    <Card title={<span className="flex items-center gap-2"><FileText className="h-3.5 w-3.5" />Vendor master record</span>}>
      <p className="mb-2 text-[11px] text-slate-500">What finance loads into the ERP: {String(m.status || "").startsWith("draft") ? "a draft until the application is approved" : `approved${m.approved_at ? ` ${timeAgo(m.approved_at)}` : ""}`}.</p>
      <dl className="space-y-1.5 text-[13px]">
        {rows.map(([k, v]) => <div key={k} className="grid grid-cols-[78px_minmax(0,1fr)] gap-2"><dt className="text-slate-500">{k}</dt><dd className="min-w-0 break-words text-slate-800">{v}</dd></div>)}
      </dl>
      {stale ? <p className="mt-3 text-xs text-amber-800">This case changed: reload the page to download the current record.</p> : (
        <div className="mt-3 flex gap-2 text-xs">
          <a className="rounded border border-slate-200 px-2 py-1 font-medium hover:bg-slate-50" href={`${base}&format=json`}>Download JSON</a>
          <a className="rounded border border-slate-200 px-2 py-1 font-medium hover:bg-slate-50" href={`${base}&format=csv`}>Download CSV</a>
        </div>
      )}
    </Card>
  );
}

// ------------------------------------------------------------------ findings
function Findings({ a }: { a: any }) {
  const [showAll, setShowAll] = useState(false);
  if (!a) return <Empty>No assessment yet — the first evaluation is running.</Empty>;
  const blockers = a.checks.filter((c: any) => c.blocking && c.status !== "satisfied");
  const counts = a.checks.reduce((m: any, c: any) => ({ ...m, [c.status]: (m[c.status] || 0) + 1 }), {});
  return (
    <div className="space-y-5">
      <div className={clsx("rounded-lg px-4 py-3 text-sm", blockers.length ? "bg-amber-50 text-amber-950" : a.outcome === "auto_reject" ? "bg-red-50 text-red-900" : "bg-emerald-50 text-emerald-900")}>
        <b>{a.summary?.headline}</b>
        <div className="mt-1 flex flex-wrap gap-3 text-xs">{Object.entries(counts).map(([k, v]: any) => <span key={k} className="flex items-center gap-1"><CheckIcon status={k} className="h-3.5 w-3.5" />{v} {CHECK_STATUS_LABEL[k]?.toLowerCase()}</span>)}</div>
      </div>
      <label className="flex items-center gap-2 text-xs text-slate-600"><input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />Show passed and not-applicable checks too</label>
      {[...GROUPS, ...[...new Set<string>(a.checks.map((c: any) => c.group))].filter((g) => !GROUPS.some(([k]) => k === g)).map((g) => [g, humanize(g)])].map(([g, label]) => {
        const items = a.checks.filter((c: any) => c.group === g && (showAll || !["satisfied", "not_applicable"].includes(c.status)));
        if (!items.length) return null;
        return (
          <div key={g}>
            <h3 className="label mb-2">{label}</h3>
            <ul className="space-y-2">{items.map((c: any) => <CheckRow key={c.key} c={c} />)}</ul>
          </div>
        );
      })}
    </div>
  );
}

function CheckRow({ c }: { c: any }) {
  const [open, setOpen] = useState(false);
  const ev = c.evidence || {};
  const high = ev.severity === "high";
  const facts = Object.entries(ev).filter(([k]) => !["fingerprint", "vendor_text", "severity", "observations", "observation", "changes", "superseded_reviews", "resolution", "problems", "unconfirmed_fields"].includes(k));
  return (
    <li className={clsx("rounded-lg border px-3.5 py-3", high && c.blocking ? "border-red-200 bg-red-50/40" : "border-slate-200")}>
      <div className="flex items-start gap-2.5">
        <CheckIcon status={c.status} className="mt-0.5" />
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-semibold text-slate-900">{c.title}</span>
            {high && c.blocking && <span className="flex items-center gap-1 rounded bg-red-100 px-1.5 py-0.5 text-[11px] font-semibold text-red-800"><ShieldAlert className="h-3 w-3" />Identity or fraud signal</span>}
            {c.blocking && c.status !== "satisfied" && <OwnerBadge owner={c.owner} />}
            <span className="text-[11px] text-slate-400">{CHECK_STATUS_LABEL[c.status]}</span>
          </div>
          <p className="mt-0.5 text-[13px] text-slate-700">{c.reason}</p>
          {ev.vendor_text && c.owner === "vendor" && c.status !== "satisfied" && <p className="mt-1.5 rounded bg-amber-50 px-2 py-1 text-xs text-amber-900"><b>Told the vendor:</b> {ev.vendor_text}</p>}
          {ev.resolution && <p className="mt-1.5 rounded bg-violet-50 px-2 py-1 text-xs text-violet-900"><b>Reviewed by {ev.resolution.by}:</b> {ev.resolution.note}</p>}
          {ev.superseded_reviews && <p className="mt-1.5 rounded bg-slate-100 px-2 py-1 text-xs text-slate-700"><b>Earlier review no longer applies</b> (evidence changed): {ev.superseded_reviews.map((s: any) => `${s.by}: “${s.note}”`).join("; ")}</p>}
          {ev.unconfirmed_fields && c.status !== "satisfied" && (
            <ul className="mt-1.5 space-y-0.5 rounded bg-sky-50 px-2 py-1.5 text-xs text-sky-900">
              {ev.unconfirmed_fields.map((f: any) => (
                <li key={f.field} className="flex flex-wrap items-center gap-1.5"><b>{humanize(f.field)}:</b><span className="mono">{f.field === "account_number" ? mask(f.value) : f.value}</span>
                  <Provenance p={f.provenance} />{f.page && <span className="text-sky-700">page {f.page}</span>}</li>))}
              <li className="pt-0.5 text-[11px] text-sky-800">Open the Evidence tab to compare these with the page image.</li>
            </ul>
          )}
          {(facts.length > 0 || ev.observations || ev.observation || ev.changes) && (
            <button onClick={() => setOpen(!open)} className="mt-1 text-xs text-brand-600 hover:underline">{open ? "Hide" : "Show"} evidence</button>
          )}
          {open && (
            <div className="mt-2 space-y-1 rounded-md bg-slate-50 p-2.5 text-xs text-slate-700">
              {facts.map(([k, v]) => <div key={k} className="flex gap-2"><span className="w-40 shrink-0 text-slate-500">{humanize(k)}</span><span className="mono break-all">{typeof v === "object" ? JSON.stringify(v) : String(v)}</span></div>)}
              {[...(ev.observations || []), ...(ev.observation ? [ev.observation] : [])].map((o: any, i: number) => (
                <div key={i} className="flex flex-wrap items-center gap-2"><FileText className="h-3.5 w-3.5 text-slate-400" />{o.source} p.{o.page ?? "?"}: <span className="mono">{o.raw}</span><Provenance p={o.provenance} /></div>
              ))}
              {(ev.changes || []).map((ch: any, i: number) => <div key={i}>{ch.label}: <span className="mono">{ch.field.includes("account_number") ? mask(ch.before) : ch.before}</span> → <span className="mono">{ch.field.includes("account_number") ? mask(ch.after) : ch.after}</span></div>)}
            </div>
          )}
        </div>
      </div>
    </li>
  );
}

// ------------------------------------------------------------------ evidence
function Evidence({ a, rev }: { a: any; rev: any }) {
  // selected by SLOT: one file can fill two slots, each with its own reading
  const [slot, setSlot] = useState<string | null>(rev?.documents[0]?.slot ?? null);
  const doc = rev?.documents.find((d: any) => d.slot === slot);
  const [page, setPage] = useState(1);
  useEffect(() => setPage(1), [slot]);
  const slotOf = (o: any) => o.slot ?? rev?.documents.find((d: any) => d.document_id === o.document_id)?.slot ?? null;
  const fields = a?.comparison?.fields || {};
  return (
    <div className="space-y-6">
      <div>
        <h3 className="label mb-2">Form vs documents</h3>
        <div className="overflow-x-auto rounded-lg border border-slate-200">
          <table className="w-full text-[13px]">
            <thead><tr className="border-b border-slate-200 bg-slate-50 text-left text-[13px] text-slate-500">
              <th className="px-3 py-2 font-medium">Field</th><th className="px-3 py-2 font-medium">Vendor entered</th><th className="px-3 py-2 font-medium">Documents say</th><th className="px-3 py-2 font-medium">Result</th></tr></thead>
            <tbody className="divide-y divide-slate-100">
              {Object.values(fields).map((f: any) => (
                <tr key={f.key} className="align-top">
                  <td className="px-3 py-2 font-medium text-slate-800">{f.label}</td>
                  <td className="mono px-3 py-2 text-slate-700">{f.key === "account_number" ? f.form_value : f.form_value || <span className="text-slate-400">—</span>}</td>
                  <td className="px-3 py-2">
                    {f.observations.length === 0 ? (f.status === "not_read"
                      ? <span className="text-xs italic text-amber-700">not read yet: the document's reading failed</span>
                      : <span className="text-xs text-slate-400">no document carries this</span>) : (
                      <ul className="space-y-1">{f.observations.map((o: any, i: number) => (
                        <li key={i} className="flex flex-wrap items-center gap-1.5">
                          <button onClick={() => { setSlot(slotOf(o)); if (o.page) setTimeout(() => setPage(o.page), 0); }} className="text-xs text-brand-700 hover:underline">{o.source.split(" (")[0]}</button>
                          {o.status === "found" ? <span className={clsx("mono", o.relation === "differs" && "font-semibold text-red-700", o.relation === "similar" && "text-amber-700")}>{o.raw}</span>
                            : <span className="text-xs italic text-amber-700">{o.status.replace("_", " ")}</span>}
                          <Provenance p={o.provenance} />
                        </li>))}
                      </ul>)}
                  </td>
                  <td className="px-3 py-2"><ResultPill s={f.status} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <div>
        <h3 className="label mb-2">Documents in revision {rev?.number}</h3>
        <div className="mb-3 flex flex-wrap gap-2">
          {rev?.documents.map((d: any) => (
            <button key={d.slot} onClick={() => setSlot(d.slot)} className={clsx("rounded-lg border px-3 py-2 text-left text-xs",
              slot === d.slot ? "border-brand-500 bg-brand-50" : "border-slate-200 hover:bg-slate-50")}>
              <span className="block font-semibold text-slate-800">{d.slot_label.split(" (")[0]}</span>
              <span className="text-slate-500">{d.filename} · {d.pages}p · {layoutLabel(d.layout)}</span>
            </button>
          ))}
        </div>
        {doc && (
          <div className="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
            <div className="rounded-lg border border-slate-200 bg-slate-100 p-2">
              <img src={`/api/documents/${doc.document_id}/page/${page}`} alt={`${doc.filename} page ${page}`} className="mx-auto max-h-[560px] w-auto rounded bg-white shadow" />
              <div className="mt-2 flex items-center justify-between text-xs text-slate-600">
                <span>Page {page} of {doc.pages} · {doc.page_kinds[page - 1] === "text" ? "has a text layer" : "image only (scanned)"}</span>
                <span className="flex gap-1">{Array.from({ length: doc.pages }, (_, i) => <button key={i} onClick={() => setPage(i + 1)} className={clsx("h-6 w-6 rounded", page === i + 1 ? "bg-slate-800 text-white" : "bg-white")}>{i + 1}</button>)}
                  <a href={`/api/documents/${doc.document_id}/file`} target="_blank" rel="noreferrer" className="ml-2 inline-block whitespace-nowrap rounded bg-white px-2 py-1 hover:underline">open PDF</a></span>
              </div>
            </div>
            <ReadingPanel doc={doc} assessed={!!rev?.assessed_by} />
          </div>
        )}
      </div>
    </div>
  );
}

function ReadingPanel({ doc, assessed }: { doc: any; assessed: boolean }) {
  const rd = doc.reading;
  if (!rd) return <Empty>{assessed ? "No reading is linked to this document in the assessment of this revision."
    : "No reading for this revision yet — it appears once this revision's evaluation finishes."}</Empty>;
  const res = rd.result || {};
  return (
    <div className="space-y-3">
      <div className="rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-600">
        <div><b className="text-slate-800">Detected:</b> {humanize(rd.detected_type || "—")} {rd.detected_type && rd.detected_type !== doc.slot && <span className="font-semibold text-red-700">(uploaded as {doc.slot_label.split(" (")[0]})</span>}</div>
        <div><b className="text-slate-800">Read by:</b> {rd.provider ? `${rd.provider} · ${rd.model}` : "—"} {rd.latency_ms ? `· ${secs(rd.latency_ms / 1000)}` : ""} · {rd.status}</div>
        {rd.provider === "test-fixture" && <div className="font-semibold text-amber-700">Test reader (gold labels) — not a live model reading</div>}
        {rd.reused_from_id && <div className="text-slate-600">Reused: an identical file was already read with the same settings (reading #{rd.reused_from_id}); no new model call was made.</div>}
        {rd.legacy_link && <div className="text-slate-600">Older record: this assessment was made before readings were linked to assessments. Shown is the latest reading of this file made before it.</div>}
        {doc.hidden_text_chars > 0 && <div className="font-medium text-amber-800">{doc.hidden_text_chars} character(s) of text in this file can't be seen on the page (covered, invisible, off the page or too faint). They were not given to the model and don't count as evidence.</div>}
        {rd.error?.message && <div className="text-red-700">Error: {rd.error.message}</div>}
        {!!res.quality_issues?.length && <div className="text-amber-800"><b>Quality issues:</b> {res.quality_issues.join("; ")}</div>}
      </div>
      <table className="w-full text-[13px]">
        <tbody className="divide-y divide-slate-100">
          {Object.values(res.fields || {}).map((f: any) => (
            <tr key={f.key} className="align-top">
              <td className="py-1.5 pr-2 text-slate-500">{humanize(f.key)}{f.critical && <span className="text-red-500">*</span>}</td>
              <td className="py-1.5">
                {f.status === "found" ? <span className="mono text-slate-900">{f.value}</span> : <span className={clsx("text-xs italic", f.status === "unreadable" ? "text-amber-700" : "text-slate-400")}>{f.status.replace("_", " ")}</span>}
                {f.alternatives?.length > 0 && <span className="ml-1 text-xs text-amber-700">alternatives: {f.alternatives.join(" / ")}</span>}
              </td>
              <td className="py-1.5 text-right"><Provenance p={f.provenance} />{f.page && <span className="ml-1 text-[11px] text-slate-400">p.{f.page}</span>}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {rd.status === "failed" ? <p className="text-[11px] text-slate-500">This document could not be read, so nothing from it counts as evidence yet. A retry reads it again.</p> :
      <p className="text-[11px] text-slate-500">The model read this document without seeing the form, so it could not simply copy the vendor's values. “text-verified” means our code found the value in the PDF's own visible text. Key values without that proof (read from a scan, or not found in the text) only count after a reviewer confirms them against the page image; matching the form is not enough.</p>}
    </div>
  );
}

function ResultPill({ s }: { s: string }) {
  const map: Record<string, [string, string]> = {
    agrees: ["agrees", "bg-emerald-50 text-emerald-800"], equivalent: ["agrees (normalised)", "bg-emerald-50 text-emerald-800"],
    similar: ["close — review", "bg-amber-50 text-amber-800"], differs: ["differs", "bg-red-50 text-red-800"],
    unconfirmed: ["unconfirmed", "bg-amber-50 text-amber-800"], not_provided: ["not entered", "bg-slate-100 text-slate-600"],
    no_evidence: ["no document", "bg-slate-100 text-slate-500"], not_read: ["not read yet", "bg-amber-50 text-amber-800"],
  };
  const [l, c] = map[s] || [s, "bg-slate-100"];
  return <span className={clsx("whitespace-nowrap rounded px-1.5 py-0.5 text-[11px] font-medium", c)}>{l}</span>;
}

// ------------------------------------------------------------------ changes
function Changes({ a, revisions }: { a: any; revisions: any[] }) {
  if (revisions.length < 2 && !a?.comparison?.changes?.length) return <Empty>Only one revision so far — nothing to compare yet.</Empty>;
  const ch = a?.comparison?.changes || [];
  const dc = a?.comparison?.doc_changes || [];
  return (
    <div className="space-y-5">
      <div>
        <h3 className="label mb-2">Field changes vs previous version</h3>
        {ch.length === 0 ? <Empty>No form fields changed.</Empty> : (
          <ul className="divide-y divide-slate-100 rounded-lg border border-slate-200">
            {ch.map((c: any, i: number) => (
              <li key={i} className={clsx("flex flex-wrap items-center gap-3 px-3 py-2 text-[13px]", c.material && "bg-amber-50/60")}>
                <span className="w-44 font-medium text-slate-800">{c.label}</span>
                <span className="mono text-red-700 line-through">{c.field.includes("account_number") ? mask(c.before) : c.before || "—"}</span>
                <span className="text-slate-400">→</span>
                <span className="mono text-emerald-800">{c.field.includes("account_number") ? mask(c.after) : c.after || "—"}</span>
                {c.material && <span className="rounded bg-amber-100 px-1.5 py-0.5 text-[11px] font-semibold capitalize text-amber-900">{c.area} change</span>}
              </li>
            ))}
          </ul>
        )}
      </div>
      <div>
        <h3 className="label mb-2">Documents</h3>
        <div className="flex flex-wrap gap-2">{dc.map((d: any) => (
          <span key={d.slot} className={clsx("rounded-md px-2 py-1 text-xs", d.change === "unchanged" ? "bg-slate-100 text-slate-600" : d.change === "added" ? "bg-emerald-50 text-emerald-800" : d.change === "removed" ? "bg-red-50 text-red-800" : "bg-amber-50 text-amber-900")}>{humanize(d.slot)}: {d.change}</span>
        ))}</div>
      </div>
      <div>
        <h3 className="label mb-2">Revisions</h3>
        <ol className="space-y-2">{[...revisions].reverse().map((r) => (
          <li key={r.id} className="rounded-lg border border-slate-200 px-3 py-2 text-[13px]">
            <div className="flex justify-between"><b>Revision {r.number}</b><span className="text-xs text-slate-500">{dateTime(r.submitted_at)}</span></div>
            {r.vendor_note && <p className="text-xs text-slate-600">Vendor note: “{r.vendor_note}”</p>}
            <p className="text-xs text-slate-500">{r.documents.map((d: any) => d.filename).join(" · ")}</p>
          </li>))}
        </ol>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ history
function HistoryTab({ data }: { data: any }) {
  const items = [
    ...data.audit.map((e: any) => ({ at: e.at, kind: "event", title: humanize(e.type.toLowerCase()), who: e.actor, detail: e.data })),
    ...data.messages.map((m: any) => ({ at: m.at, kind: "message", title: `Message to vendor: ${m.subject}`, who: "portal", detail: m.items?.length ? `${m.items.length} item(s)` : null })),
    ...data.decisions.map((d: any) => ({ at: d.at, kind: "decision", title: `Decision: ${d.outcome}${d.rule ? ` (rule: ${d.rule})` : ""}`, who: d.actor, detail: d.reason })),
    ...data.resolutions.map((r: any) => ({ at: r.at, kind: "review", title: `Review ${r.outcome.replace("_", " ")}: ${humanize(r.check_key.split(":").pop())}`, who: r.actor, detail: [r.note, r.confirmation ? `Confirmed by ${r.confirmation.method_label} with ${r.confirmation.contact_person} (${r.confirmation.contact_source_label})` : null, r.vendor_text ? `Told the vendor: ${r.vendor_text}` : null].filter(Boolean).join(" · ") })),
    ...data.verifications.map((v: any) => ({ at: v.at, kind: "verification", title: `Registry ${v.result.replace("_", " ")} for ${v.identifier}`, who: `${v.actor} · ${v.method.replace("_", " ")}`, detail: v.observed?.legal_name })),
  ].sort((x, y) => (x.at < y.at ? 1 : -1));
  return (
    <div className="grid grid-cols-1 gap-5 lg:grid-cols-[minmax(0,1fr)_260px]">
      <ol className="relative min-w-0 space-y-3 border-l border-slate-200 pl-4">
        {items.map((it, i) => (
          <li key={i} className="relative">
            <span className={clsx("absolute -left-[21px] top-1.5 h-2.5 w-2.5 rounded-full ring-2 ring-white",
              it.kind === "decision" ? "bg-slate-900" : it.kind === "message" ? "bg-brand-500" : it.kind === "review" ? "bg-violet-500" : it.kind === "verification" ? "bg-teal-500" : "bg-slate-300")} />
            <div className="flex flex-wrap items-baseline justify-between gap-2"><span className="text-[13px] font-medium text-slate-800">{it.title}</span><span className="text-[11px] text-slate-400">{dateTime(it.at)}</span></div>
            <p className="text-xs text-slate-500">{it.who}{it.detail ? ` — ${typeof it.detail === "object" ? Object.entries(it.detail).map(([k, v]) => `${k}: ${Array.isArray(v) ? v.join(", ") : v}`).join(" · ") : it.detail}` : ""}</p>
          </li>
        ))}
      </ol>
      <div>
        <h3 className="label mb-2">Runs</h3>
        <ul className="space-y-1.5">{data.runs.map((x: any) => (
          <li key={x.id} className="flex items-center justify-between rounded-md bg-slate-50 px-2.5 py-1.5 text-xs">
            <span className="mono">#{x.id}</span><span className="text-slate-600">{x.trigger.replace(/_/g, " ")}</span><RunStatus status={x.status} /><span className="text-slate-500">{secs(x.duration_s)}</span>
          </li>))}
        </ul>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------ decision + tasks
function useAct(id: number, onDone: () => void, version?: number) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<any>(null);
  // A reload that brings a newer version of the case answers the earlier refusal ("changed while you were looking"):
  // don't leave that error standing over the current evidence.
  useEffect(() => setErr(null), [version]);
  const act = async (action: string, body: any) => {
    setBusy(true); setErr(null);
    try { await api(`/review/requests/${id}/${action}`, { body: { operation_id: opId(), ...body } }); onDone(); return true; }
    catch (e) { setErr(e); return false; } finally { setBusy(false); }
  };
  return { busy, err, act, setErr };
}

function DecisionPanel({ data, onDone }: { data: any; onDone: () => void }) {
  const r = data.request;
  const { busy, err, act } = useAct(r.id, onDone, r.version);
  const [mode, setMode] = useState<"none" | "approve" | "reject">("none");
  const [note, setNote] = useState("");
  const [reason, setReason] = useState("");
  const [explain, setExplain] = useState("");
  const closed = r.state === "APPROVED" || r.state === "REJECTED";
  const last = data.decisions[data.decisions.length - 1];
  const assessedNow = data.assessment && data.assessment.revision_id === data.current_revision_id;
  const outdated = assessedNow && data.assessment.engine_current === false;
  const { me } = useAuth();
  const risk = data.assessment?.summary?.risk;
  const order: Record<string, number> = { low: 0, medium: 1, high: 2, never: 9 };
  const twoPeople = !!risk && order[risk.tier] >= order[data.approval_rules?.second_approver_from_risk ?? "never"];
  const firstIsMe = !!data.second_approval && data.second_approval.first_by_id === me?.id;
  const canApprove = r.state === "READY_FOR_DECISION" && !outdated && !firstIsMe;
  const canReject = (r.state === "ACTION_REQUIRED" || r.state === "READY_FOR_DECISION") && assessedNow;
  return (
    <Card title={<span className="flex items-center gap-2"><Gavel className="h-3.5 w-3.5" />Decision</span>}>
      {closed && last ? (
        <div className={clsx("rounded-lg px-3 py-2.5 text-sm", last.outcome === "approved" ? "bg-emerald-50 text-emerald-900" : "bg-red-50 text-red-900")}>
          <b>{last.outcome === "approved" ? "Approved" : "Rejected"}</b>{" "}
          {last.actor_type === "system"
            ? (last.rule === "auto_approve_clean" ? "automatically, by the buyer's policy" : <>by policy rule <span className="mono">{last.rule}</span></>)
            : <>by {last.actor}</>}, {dateTime(last.at)}
          <p className="mt-1 text-[13px]">{last.reason}</p>
        </div>
      ) : (
        <div className="space-y-3">
          {outdated && !closed && r.state !== "PROCESSING" && (
            <div className="space-y-2 rounded-lg bg-sky-50 px-3 py-2.5 text-[13px] text-sky-900">
              <p>This assessment was made under an earlier version of the checks, so it can't be approved. Re-evaluate it with the current checks first (the documents and history are kept).</p>
              <RetryButton r={r} onDone={onDone} label="Re-evaluate with current checks" />
            </div>
          )}
          <p className="text-sm text-slate-700">{r.state === "READY_FOR_DECISION" && !outdated ? "All required checks are satisfied. Approve to create the vendor's approved profile." :
            r.state === "PROCESSING" ? "Evaluation in progress — decisions unlock when it finishes." :
            !assessedNow ? "This revision has no completed evaluation yet (see the retry task). Decisions need evidence, so retry the evaluation first." :
            "Approval is blocked until the open tasks are resolved. You can still reject with a reason."}</p>
          {twoPeople && !closed && r.state === "READY_FOR_DECISION" && (
            <p className="rounded-lg bg-violet-50 px-3 py-2 text-[13px] text-violet-900">
              {data.second_approval
                ? <>First approval by <b>{data.second_approval.first_by}</b> ({timeAgo(data.second_approval.at)}){data.second_approval.note ? `: “${data.second_approval.note}”` : ""}. {firstIsMe ? "A second, different person must now approve." : "Your approval completes it."}</>
                : <>This supplier is <b>{risk.tier} risk</b> ({risk.score}/100), so two different people must approve. The first approval is recorded; the second one decides.</>}
            </p>
          )}
          {mode === "none" && (
            <div className="flex gap-2">
              <Button variant="success" disabled={!canApprove || busy} onClick={() => setMode("approve")}><BadgeCheck className="h-4 w-4" />Approve</Button>
              <Button variant="danger" disabled={!canReject || busy} onClick={() => setMode("reject")}><X className="h-4 w-4" />Reject</Button>
            </div>
          )}
          {mode === "approve" && (
            <div className="space-y-2">
              <textarea className={clsx(inputCls, "min-h-16")} placeholder="Approval note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
              <p className="text-[11px] text-slate-500">Approving revision {r.revision} · case version v{r.version} · assessment #{data.assessment?.id}. If anything changed since you loaded this page, the server will refuse.</p>
              <div className="flex gap-2">
                <Button variant="success" disabled={busy} onClick={async () => { if (await act("approve", { expected_version: r.version, assessment_id: data.assessment?.id, note })) setMode("none"); }}><Check className="h-4 w-4" />Confirm approval</Button>
                <Button variant="ghost" onClick={() => setMode("none")}>Cancel</Button>
              </div>
            </div>
          )}
          {mode === "reject" && (
            <div className="space-y-2">
              <textarea className={clsx(inputCls, "min-h-16")} placeholder="Internal reason (recorded in the audit trail)" value={reason} onChange={(e) => setReason(e.target.value)} />
              <textarea className={clsx(inputCls, "min-h-16")} placeholder="Explanation shown to the vendor" value={explain} onChange={(e) => setExplain(e.target.value)} />
              <div className="flex gap-2">
                <Button variant="danger" disabled={busy} onClick={async () => { if (await act("reject", { expected_version: r.version, reason, vendor_explanation: explain })) setMode("none"); }}>Confirm rejection</Button>
                <Button variant="ghost" onClick={() => setMode("none")}>Cancel</Button>
              </div>
            </div>
          )}
          {err && (
            <div className="space-y-2">
              <ErrorNote error={err} />
              {(err.code === "stale_view" || err.code === "stale_assessment" || err.code === "outdated_assessment") && <Button variant="secondary" size="sm" onClick={onDone}><RefreshCw className="h-3.5 w-3.5" />Reload latest version</Button>}
            </div>
          )}
        </div>
      )}
    </Card>
  );
}

function TasksPanel({ data, tasks, onDone }: { data: any; tasks: any[]; onDone: () => void }) {
  const { me } = useAuth();
  const r = data.request;
  const checks = data.assessment?.checks || [];
  // A stored reading of the current revision's file that is missing key information (status "partial").
  const incomplete = (slot: string) => data.assessment?.revision_id === data.current_revision_id
    && data.assessment?.summary?.documents?.[slot]?.status === "partial";
  return (
    <Card title={`Open tasks (${tasks.length})`}>
      {tasks.length === 0 ? <Empty>No open tasks.</Empty> : (
        <ul className="space-y-3">
          {tasks.map((t: any) => {
            const reviewChecks = checks.filter((c: any) => c.task_key === t.key && c.status === "needs_review");
            return (
              <li key={t.id} className="rounded-lg border border-slate-200 p-3">
                <div className="mb-1 flex items-center gap-2"><OwnerBadge owner={t.owner} /><span className="text-sm font-semibold text-slate-900">{t.title}</span></div>
                <p className="whitespace-pre-line text-xs text-slate-600">{t.detail}</p>
                {t.owner === "vendor" && <p className="mt-1.5 text-[11px] text-slate-500">Waiting for the vendor — they were sent a specific request.</p>}
                {t.key.startsWith("vendor:upload:") && incomplete(t.key.slice("vendor:upload:".length)) &&
                  <ReReadButton r={r} slot={t.key.slice("vendor:upload:".length)} onDone={onDone} />}
                {t.owner === "system" && <RetryButton r={r} onDone={onDone} />}
                {/* Keyed by the evidence too: when the evidence changes (e.g. the supplier changed the account), half-filled
                    reasoning and confirmations about the OLD evidence are cleared instead of being applied to the new one. */}
                {reviewChecks.map((c: any) => <ReviewBox key={`${c.key}:${c.evidence?.fingerprint ?? ""}`} c={c} r={r} me={me} onDone={onDone} showReason={reviewChecks.length > 1} />)}
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

function RetryButton({ r, onDone, label }: { r: any; onDone: () => void; label?: string }) {
  const { busy, err, act } = useAct(r.id, onDone, r.version);
  return (
    <div className="mt-2 space-y-2">
      <Button size="sm" variant="secondary" disabled={busy || (label ? r.state === "PROCESSING" : r.state !== "ACTION_REQUIRED")} onClick={() => act("retry", { expected_version: r.version })}><RotateCcw className="h-3.5 w-3.5" />{label || "Retry processing (same revision)"}</Button>
      <ErrorNote error={err} />
    </div>
  );
}

function ReReadButton({ r, slot, onDone }: { r: any; slot: string; onDone: () => void }) {
  const { busy, err, act } = useAct(r.id, onDone, r.version);
  return (
    <div className="mt-2 space-y-1.5 rounded-md bg-slate-50 p-2.5">
      <p className="text-[11px] text-slate-600">The stored reading of this file is missing key information. If the page clearly shows it, the reader may have missed it: read the file again instead of waiting on the vendor. Only this file is read (one model call, recorded in the history); other files keep their current readings.</p>
      <Button size="sm" variant="secondary" disabled={busy || r.state === "PROCESSING"} onClick={() => act("reread", { expected_version: r.version, slot })}><RefreshCw className="h-3.5 w-3.5" />Re-read this document</Button>
      <ErrorNote error={err} />
    </div>
  );
}

function ReviewBox({ c, r, me, onDone, showReason }: { c: any; r: any; me: any; onDone: () => void; showReason: boolean }) {
  const { busy, err, act } = useAct(r.id, onDone, r.version);
  const [note, setNote] = useState("");
  const [vendorText, setVendorText] = useState("");
  // A bank change is accepted only with a record of how it was confirmed with the vendor, on a contact the buyer
  // already held (the server refuses it otherwise).
  const bankChange = c.key === "change:bank";
  const [method, setMethod] = useState("");
  const [source, setSource] = useState("");
  const [person, setPerson] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const confirmationReady = !bankChange || (method && source && person.trim().length >= 2 && confirmed);
  const financeOnly = c.owner === "finance" && me?.role !== "finance";
  const go = (outcome: string) => act("resolve", { expected_version: r.version, check_key: c.key, fingerprint: c.evidence?.fingerprint, outcome, note,
                                                   vendor_text: outcome === "not_accepted" ? vendorText : null,
                                                   confirmation: bankChange && outcome === "accepted" ? { method, contact_source: source, contact_person: person, confirmed } : null });
  const confirming = c.key.startsWith("confirm:");
  return (
    <div className="mt-2 rounded-md bg-violet-50/60 p-2.5">
      {showReason && <p className="mb-1.5 text-xs text-violet-900">{c.reason}</p>}
      {financeOnly ? <p className="text-[11px] font-medium text-teal-800">Needs a finance reviewer — sign in as Finance to clear this.</p> : (
        <>
          <input className={clsx(inputCls, "py-1.5 text-xs")} placeholder={confirming ? "e.g. Checked against page 1 — all values match (internal, recorded)" : "Your reasoning (internal, recorded; never shown to the vendor)"} value={note} onChange={(e) => setNote(e.target.value)} />
          {bankChange && (
            <fieldset className="mt-2 space-y-1.5 rounded border border-teal-200 bg-white/70 p-2">
              <legend className="px-1 text-[11px] font-semibold text-teal-900">To accept: how you confirmed the change with the vendor</legend>
              <select aria-label="Confirmation method" className={clsx(inputCls, "py-1.5 text-xs")} value={method} onChange={(e) => setMethod(e.target.value)}>
                <option value="">How did you confirm it?</option>
                <option value="call_back">Call-back</option>
                <option value="email_on_file">E-mail to an address on file</option>
                <option value="in_person">In person</option>
              </select>
              <select aria-label="Where the contact came from" className={clsx(inputCls, "py-1.5 text-xs")} value={source} onChange={(e) => setSource(e.target.value)}>
                <option value="">Where did the contact come from?</option>
                <option value="approved_record">The vendor's approved record, from before this change</option>
                <option value="contract">The signed contract or earlier purchase orders</option>
                <option value="independent_lookup">The company's number, looked up independently</option>
              </select>
              <input className={clsx(inputCls, "py-1.5 text-xs")} placeholder="Who confirmed it (name and role)" value={person} onChange={(e) => setPerson(e.target.value)} />
              <label className="flex items-start gap-1.5 text-[11px] text-slate-700"><input type="checkbox" className="mt-0.5" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} />They confirmed the new account number and IFSC. (Never use a phone number or e-mail from this application.)</label>
              <p className="text-[10px] text-slate-500">Whoever accepts the bank change can't also give the final approval.</p>
            </fieldset>
          )}
          <input className={clsx(inputCls, "mt-1.5 py-1.5 text-xs")} placeholder="If not acceptable: what the vendor should fix or provide (shown to the vendor)" value={vendorText} onChange={(e) => setVendorText(e.target.value)} />
          <div className="mt-2 flex gap-2">
            <Button size="sm" variant="success" disabled={busy || note.trim().length < 5 || !confirmationReady} onClick={() => go("accepted")}><Check className="h-3.5 w-3.5" />{confirming ? "Values match the page" : "Accept"}</Button>
            <Button size="sm" variant="danger" disabled={busy || note.trim().length < 5 || vendorText.trim().length < 5} onClick={() => go("not_accepted")} title="The vendor is asked to fix it and sees only the second box; your reasoning stays internal">{confirming ? "Don't match / unreadable" : "Not acceptable"}</Button>
          </div>
          <p className="mt-1 text-[10px] text-slate-500">Your decision is bound to this exact evidence. If the vendor later changes it, the review must be repeated.</p>
        </>
      )}
      <ErrorNote error={err} />
    </div>
  );
}

function RegistryPanel({ data, onDone }: { data: any; onDone: () => void }) {
  const r = data.request;
  const rev = data.revisions.find((x: any) => x.id === data.current_revision_id);
  const gstin = rev?.form?.tax?.gstin;
  const needsManual = data.tasks.some((t: any) => t.status === "open" && (t.key === "reviewer:registry" || t.key === "system:registry"));
  // A mistaken lookup (e.g. "No record found" by accident) must be correctable: while the case is open, a reviewer can
  // record another lookup whenever the policy accepts manual ones. The latest result counts; earlier ones stay listed.
  const manualAccepted = (data.policy?.verification || []).some((v: any) => v.identifier === "GSTIN" && v.accepted_methods.includes("manual_registry"));
  const { busy, err, act } = useAct(r.id, onDone, r.version);
  const [result, setResult] = useState("found");
  const [legal, setLegal] = useState("");
  const [status, setStatus] = useState("Active");
  const [ttype, setTtype] = useState("");
  const [missed3b, setMissed3b] = useState("");
  const [missed1, setMissed1] = useState("");
  // Only GST lookups here: bank verifications appear as findings (and in the vendor master record).
  const verifs = useMemo(() => [...data.verifications].filter((v: any) => !v.type || v.type === "GSTIN").reverse(), [data.verifications]);
  // A recorded lookup starts the next one from a clean form (typing used to add to the previous name).
  useEffect(() => { setResult("found"); setLegal(""); setStatus("Active"); setTtype(""); setMissed3b(""); setMissed1(""); }, [data.verifications.length]);
  const applicant = rev?.form?.business?.legal_name || "";
  const record = () => {
    // A typo here goes straight to the supplier as a mismatch: check before recording a name that differs.
    const norm = (x: string) => x.toLowerCase().replace(/pvt/g, "private").replace(/ltd/g, "limited").replace(/[^a-z0-9]/g, "");
    if (result === "found" && applicant && norm(legal) !== norm(applicant)
        && !window.confirm(`The name you entered ("${legal.trim()}") differs from the application's legal name ("${applicant}"). `
                           + "If you record it, the supplier will be asked to explain the difference. Record it anyway?")) return;
    act("verification", { expected_version: r.version, identifier: gstin, result, legal_name: legal, status,
      taxpayer_type: ttype || null, gstr3b_missed: missed3b === "" ? null : Number(missed3b), gstr1_missed: missed1 === "" ? null : Number(missed1) });
  };
  if (!gstin && verifs.length === 0) return null;
  return (
    <Card title={<span className="flex items-center gap-2"><Landmark className="h-3.5 w-3.5" />GST registry</span>}>
      {verifs.length === 0 ? <p className="text-sm text-slate-500">No lookup recorded yet.</p> : (
        <ul className="space-y-2">
          {verifs.map((v: any) => (
            <li key={v.id} className={clsx("rounded-md border px-2.5 py-2 text-xs", v.identifier !== gstin ? "border-slate-200 opacity-60" : "border-slate-200")}>
              <div className="flex items-center justify-between gap-2">
                <span className="mono font-medium">{v.identifier}</span>
                <span className={clsx("rounded px-1.5 py-0.5 text-[11px] font-bold", v.method === "simulated_registry" ? "bg-amber-100 text-amber-900" : "bg-teal-100 text-teal-900")}>{v.method === "simulated_registry" ? "Simulated" : "Manual lookup"}</span>
              </div>
              <div className="mt-1 text-slate-700">{v.result.replace("_", " ")}{v.observed?.legal_name ? `: ${v.observed.legal_name} (${v.observed.status})` : ""}
                {v.observed?.taxpayer_type && <> · {v.observed.taxpayer_type} taxpayer</>}
                {Array.isArray(v.observed?.filings) && <> · GSTR-3B not filed: {v.observed.filings.slice(-12).filter((f: any) => !f.gstr3b_filed_on).length} of 12 months</>}
                {v.observed?.gstr3b_missed != null && <> · GSTR-3B not filed: {v.observed.gstr3b_missed} of 12 months</>}
                {v.observed?.gstr1_missed != null && <> · GSTR-1 not filed: {v.observed.gstr1_missed} of 12 months</>}</div>
              <div className="text-slate-400">{v.source} · {v.actor} · {timeAgo(v.at)}{v.identifier !== gstin ? " · different GSTIN — ignored" : ""}</div>
            </li>
          ))}
        </ul>
      )}
      {(needsManual || manualAccepted) && gstin && (r.state === "ACTION_REQUIRED" || r.state === "READY_FOR_DECISION") && (
        <details open={needsManual} className="mt-3 space-y-2 border-t border-slate-100 pt-3">
          <summary className="cursor-pointer text-xs font-medium text-slate-700">{verifs.length ? "Record another lookup" : "Record a lookup"}</summary>
          <p className="mt-2 text-xs text-slate-600">Look up <span className="mono">{gstin}</span> on the official GST portal (taxpayer search) and record what it shows.{verifs.length ? " The newest result replaces earlier ones; they stay in the list." : ""}</p>
          <select aria-label="Lookup result" className={clsx(inputCls, "py-1.5 text-xs")} value={result} onChange={(e) => setResult(e.target.value)}>
            <option value="found">Record found</option><option value="not_found">No record found</option><option value="unavailable">Portal unavailable</option>
          </select>
          {result === "found" && (
            <div className="grid grid-cols-[minmax(0,1fr)_110px] gap-2">
              <input aria-label="Legal name shown on the portal" className={clsx(inputCls, "py-1.5 text-xs")} placeholder="Legal name shown" value={legal} onChange={(e) => setLegal(e.target.value)} />
              <select aria-label="Registration status shown on the portal" className={clsx(inputCls, "py-1.5 text-xs")} value={status} onChange={(e) => setStatus(e.target.value)}><option>Active</option><option>Cancelled</option><option>Suspended</option><option>Inactive</option></select>
              <select aria-label="Taxpayer type shown on the portal" className={clsx(inputCls, "py-1.5 text-xs")} value={ttype} onChange={(e) => setTtype(e.target.value)}>
                <option value="">Taxpayer type (optional)</option><option>Regular</option><option>Composition</option></select>
              <span />
              <p className="col-span-2 text-[11px] text-slate-500">Return filing (optional): fill in both counts, 0 if all were filed, or leave both blank to keep the counts recorded earlier.</p>
              <label className="col-span-2 flex items-center justify-between gap-2 text-[11px] text-slate-600">Months in the last 12 with GSTR-3B not filed
                <input aria-label="Months with GSTR-3B not filed" inputMode="numeric" className={clsx(inputCls, "w-16 py-1 text-xs")} value={missed3b} onChange={(e) => setMissed3b(e.target.value.replace(/\D/g, "").slice(0, 2))} /></label>
              <label className="col-span-2 flex items-center justify-between gap-2 text-[11px] text-slate-600">Months in the last 12 with GSTR-1 not filed
                <input aria-label="Months with GSTR-1 not filed" inputMode="numeric" className={clsx(inputCls, "w-16 py-1 text-xs")} value={missed1} onChange={(e) => setMissed1(e.target.value.replace(/\D/g, "").slice(0, 2))} /></label>
            </div>
          )}
          <Button size="sm" disabled={busy || (result === "found" && (legal.trim().length < 3 || (missed3b === "") !== (missed1 === "")))} onClick={record}>Record lookup</Button>
          <ErrorNote error={err} />
        </details>
      )}
    </Card>
  );
}

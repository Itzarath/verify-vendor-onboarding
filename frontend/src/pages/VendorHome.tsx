import clsx from "clsx";
import { History, Info, Mail, PencilLine, RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";
import RunView from "../components/RunView";
import { Button, Card, ErrorNote, Spinner, Stamp } from "../components/ui";
import { dateTime, timeAgo } from "../lib/format";
import { useResource } from "../lib/hooks";
import ApplicationForm from "./ApplicationForm";

const HEADLINE: Record<string, { title: string; text: string; tone: string }> = {
  PROCESSING: { title: "Checking your application", text: "We’re reading your documents and checking them against the buyer’s requirements. This page updates as each step finishes.", tone: "sky" },
  ACTION_REQUIRED: { title: "Not approved yet", text: "Your application can’t be approved yet. Below is what’s needed, and who is working on it.", tone: "amber" },
  READY_FOR_DECISION: { title: "Waiting for final approval", text: "Our checks and reviews are complete, with nothing left for you to do. A procurement reviewer makes the final decision.", tone: "violet" },
  APPROVED: { title: "You’re an approved supplier", text: "This covers supplier registration only. It is not a purchase order or an authorisation to pay.", tone: "emerald" },
  REJECTED: { title: "Not accepted", text: "Your application was not accepted. The reason is in your messages.", tone: "red" },
};

type Draft = { form: any; docs: Record<string, any>; revision: number; highlight: string[] };

export default function VendorHome() {
  const [poll, setPoll] = useState<number | null>(null);
  const { data, reload, loading, error } = useResource<any>("/vendor/overview", poll);
  // A correction is a SNAPSHOT of the revision shown when "Correct" was clicked: its contents and its base revision
  // never change underneath the vendor (e.g. when a reply arrives late, or another tab submits a newer revision).
  // If the application moved on meanwhile, the server refuses the stale base instead of silently undoing the change.
  const [draft, setDraft] = useState<Draft | null>(null);
  const editing = draft !== null;
  const c = data?.current;
  const processing = c?.state === "PROCESSING";
  const open = !!c && c.state !== "APPROVED" && c.state !== "REJECTED";
  // Fast while checks run; slower while waiting on the buyer, so their decision appears without a reload.
  useEffect(() => setPoll(editing ? null : processing ? 3000 : open ? 8000 : null), [processing, open, editing]);
  const startCorrection = () => setDraft({
    form: structuredClone(c.latest?.form ?? {}),
    docs: Object.fromEntries((c.latest?.documents || []).map((d: any) => [d.slot, d])),
    revision: c.revision,
    highlight: c.vendor_tasks.map((t: any) => t.key),
  });

  if (loading && !data) return <div className="flex justify-center py-20"><Spinner className="h-6 w-6 text-slate-400" /></div>;
  // A failed load is not "no application yet": never show a blank first application in its place.
  if (!data) {
    return (
      <Card title="Couldn't load your application">
        <div className="space-y-3">
          <ErrorNote error={error} />
          <Button variant="secondary" onClick={reload}><RotateCcw className="h-4 w-4" />Try again</Button>
        </div>
      </Card>
    );
  }

  if (!c || editing) {
    return (
      <div className="mx-auto max-w-4xl">
        <div className="mb-5">
          <h1 className="text-[1.9rem] font-bold leading-tight text-slate-900">{c ? "Correct your application" : "Your supplier application"}</h1>
          <p className="mt-1 text-[15px] text-slate-600">Applying to <b>{data.buyer}</b> as <b>{data.vendor.name}</b> <span className="mono text-slate-500">{data.vendor.reference}</span></p>
        </div>
        <ApplicationForm
          initialForm={draft?.form}
          initialDocs={draft?.docs}
          basedOnRevision={draft?.revision}
          highlight={draft?.highlight ?? []}
          onSubmitted={() => { setDraft(null); reload(); }}
          onCancel={editing ? () => { setDraft(null); reload(); } : undefined}
        />
      </div>
    );
  }

  const h = HEADLINE[c.state];
  const stamp = c.state === "PROCESSING" ? "processing" : c.state === "APPROVED" ? "approved" : c.state === "REJECTED" ? "rejected" : "pending";
  return (
    <div className="grid grid-cols-1 gap-6 lg:grid-cols-[minmax(0,1fr)_380px]">
      <div className="min-w-0 space-y-6">
        <section className={clsx("relative overflow-hidden rounded-lg border border-slate-200 bg-white p-6 pl-7 sm:p-7 sm:pl-8",
          "before:absolute before:inset-y-0 before:left-0 before:w-1.5",
          h.tone === "sky" && "before:bg-sky-500", h.tone === "amber" && "before:bg-amber-500", h.tone === "violet" && "before:bg-violet-500",
          h.tone === "emerald" && "before:bg-emerald-600", h.tone === "red" && "before:bg-red-600")}>
          <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-4">
            <div className="min-w-0 max-w-xl">
              <p className="text-sm text-slate-500"><span className="mono text-slate-700">{c.reference}</span>, revision {c.revision}</p>
              <h1 className="mt-1 text-[1.9rem] font-bold leading-tight text-slate-900">{h.title}</h1>
              <p className="mt-2 text-[15px] text-slate-700">{h.text}</p>
            </div>
            <Stamp status={stamp} size="lg" className="mt-1" />
          </div>
          {c.state === "APPROVED" && (
            <p className="mt-5 flex items-center gap-2 rounded-md bg-slate-50 px-3 py-2 text-sm text-slate-700"><Info className="h-4 w-4 shrink-0" />Need to change your details later? Contact the buyer’s procurement team: changes to an approved profile aren’t part of this demo.</p>
          )}
          {c.state === "ACTION_REQUIRED" && c.vendor_tasks.length === 0 && (
            <p className="mt-5 flex items-center gap-2 rounded-md bg-slate-50 px-3 py-2 text-sm text-slate-700"><Info className="h-4 w-4 shrink-0" />Nothing is needed from you right now: the buyer’s team is reviewing.</p>
          )}
          {c.can_correct && !processing && (
            <div className="mt-5"><Button variant={c.vendor_tasks.length ? "primary" : "secondary"} onClick={startCorrection}><PencilLine className="h-4 w-4" />Correct and resubmit</Button></div>
          )}
        </section>

        {/* While a correction is being checked, the open tasks still belong to the previous submission: hide them
            until the new result is in, so they don't read as the outcome of the correction. */}
        {c.vendor_tasks.length > 0 && !processing && (
          <Card title="What we need from you">
            <ol className="space-y-3">
              {c.vendor_tasks.map((t: any, i: number) => (
                <li key={t.key} className="flex gap-3">
                  <span className="display flex h-7 w-7 shrink-0 items-center justify-center rounded-full border-2 border-amber-500 text-sm font-bold text-amber-700">{i + 1}</span>
                  <div className="min-w-0">
                    <p className="text-[15px] font-semibold text-slate-900">{t.title}</p>
                    {(t.lines.length ? t.lines.slice(0, 1) : []).map((l: string, j: number) => <p key={j} className="text-sm text-slate-700">{l}</p>)}
                    {t.lines.length > 1 && (
                      <details className="mt-1 text-xs text-slate-500"><summary className="cursor-pointer">Why we're asking ({t.lines.length - 1} related finding{t.lines.length > 2 ? "s" : ""})</summary>
                        <ul className="mt-1 list-disc space-y-0.5 pl-4">{t.lines.slice(1).map((l: string, j: number) => <li key={j}>{l}</li>)}</ul>
                      </details>
                    )}
                  </div>
                </li>
              ))}
            </ol>
            {c.can_correct && <div className="mt-5"><Button onClick={startCorrection}><PencilLine className="h-4 w-4" />Fix these and resubmit</Button></div>}
          </Card>
        )}

        {c.last_run && (
          <Card title="Live processing">
            <RunView runId={c.last_run.id} onFinished={reload} compact />
          </Card>
        )}
      </div>

      <div className="min-w-0 space-y-5">
        <Card title={<span className="flex items-center gap-2"><Mail className="h-4 w-4 text-slate-400" />Messages</span>} pad={false}>
          <ul className="max-h-[520px] divide-y divide-slate-200 overflow-y-auto">
            {c.messages.map((m: any, i: number) => (
              <li key={i} className="px-5 py-4">
                <div className="mb-1 flex items-center justify-between gap-2">
                  <span className="text-sm font-semibold text-slate-900">{m.subject}</span>
                  <span className="shrink-0 text-[11px] text-slate-400" title={dateTime(m.at)}>{timeAgo(m.at)}</span>
                </div>
                <p className="text-sm text-slate-600">{m.body}</p>
                {!!m.items?.length && (
                  <ul className="mt-2 space-y-1.5">
                    {m.items.map((it: any, j: number) => (
                      <li key={j} className={clsx("rounded-md px-2.5 py-1.5 text-[13px]",
                        it.type === "action" && "bg-amber-50 text-amber-950", it.type === "info" && "bg-slate-50 text-slate-700",
                        it.type === "accepted" && "bg-emerald-50 text-emerald-900")}>
                        {it.title && <b className="block">{it.title}</b>}{it.text}
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            ))}
          </ul>
        </Card>
        <Card title={<span className="flex items-center gap-2"><History className="h-4 w-4 text-slate-400" />Your submissions</span>}>
          <ol className="space-y-3">
            {[...c.revisions].reverse().map((r: any) => (
              <li key={r.number} className="text-sm">
                <div className="flex justify-between"><span className="font-medium text-slate-800">Revision {r.number}</span><span className="text-xs text-slate-400">{dateTime(r.submitted_at)}</span></div>
                <p className="text-xs text-slate-500">{r.documents.map((d: any) => d.filename).join(", ")}</p>
              </li>
            ))}
          </ol>
        </Card>
      </div>
    </div>
  );
}

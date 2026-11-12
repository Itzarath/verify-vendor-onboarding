import clsx from "clsx";
import { CheckCircle2, FileUp, FlaskConical, Loader2, Paperclip, RefreshCw, Send, X } from "lucide-react";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import { Button, Card, ErrorNote, Field, inputCls } from "../components/ui";
import { api, opId } from "../lib/api";
import { layoutLabel } from "../lib/format";
import { useResource } from "../lib/hooks";

type Docs = Record<string, { document_id: number; filename: string; pages: number; layout: string } | undefined>;

const EMPTY = {
  business: { legal_name: "", trade_name: "", entity_type: "", registration_number: "", incorporation_date: "",
    registered_address: { line1: "", city: "", state: "", pincode: "" } },
  service: { category: "", description: "", onsite_work: "", buyer_contact: "" },
  contact: { name: "", role: "", email: "", phone: "", signatory_name: "", signatory_designation: "" },
  tax: { pan: "", gst_registered: "", gstin: "", msme_registered: "", udyam_number: "" },
  bank: { account_holder: "", account_number: "", ifsc: "", bank_name: "", branch: "" },
  declarations: { information_correct: false, conduct_acceptance: "", conflict_of_interest: "", conflict_details: "" },
};

function get(o: any, path: string) { return path.split(".").reduce((a, k) => (a ? a[k] : undefined), o); }
function setPath(o: any, path: string, v: any) {
  const c = structuredClone(o); const parts = path.split("."); let cur = c;
  for (const p of parts.slice(0, -1)) cur = cur[p] = cur[p] ?? {};
  cur[parts[parts.length - 1]] = v; return c;
}
function merge(base: any, over: any): any {
  if (!over || typeof over !== "object") return over ?? base;
  const out: any = Array.isArray(base) ? [...base] : { ...base };
  for (const k of Object.keys(over)) out[k] = typeof over[k] === "object" && over[k] !== null && !Array.isArray(over[k]) ? merge(base?.[k] ?? {}, over[k]) : (over[k] ?? "");
  return out;
}
/** Convert UI strings back to the API shape: empty -> null, keep identifiers as strings. */
function clean(o: any): any {
  if (typeof o === "string") return o.trim() === "" ? null : o.trim();
  if (o && typeof o === "object") { const r: any = {}; for (const k of Object.keys(o)) r[k] = clean(o[k]); return r; }
  return o;
}

export default function ApplicationForm({ initialForm, initialDocs, basedOnRevision, highlight = [], onSubmitted, onCancel }: {
  initialForm?: any; initialDocs?: Docs; basedOnRevision?: number; highlight?: string[]; onSubmitted: (r: any) => void; onCancel?: () => void;
}) {
  const { data: cfg, error: cfgErr, reload: reloadCfg } = useResource<any>("/config/form");
  const { data: samples } = useResource<any[]>("/samples");
  const [form, setForm] = useState<any>(() => merge(EMPTY, initialForm || {}));
  const [docs, setDocs] = useState<Docs>(initialDocs || {});
  const [note, setNote] = useState("");
  const [err, setErr] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState<Set<string>>(new Set());
  const op = useRef(opId()); // one operation id per form session: a double-click resubmits the SAME operation
  const correction = basedOnRevision !== undefined;

  // Unsubmitted work lives only in this page: warn before a refresh or a closed tab throws it away.
  const start = useRef({ form, docs, note });
  const dirty = form !== start.current.form || docs !== start.current.docs || note !== start.current.note;
  useEffect(() => {
    if (!dirty) return;
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const f = (path: string) => ({ value: get(form, path) ?? "", onChange: (e: any) => setForm(setPath(form, path, e.target.value)) });

  const [sampleLoading, setSampleLoading] = useState(false);
  const loadSample = async (key: string) => {
    if (!key) return;
    setErr(null); setBusy(true); setSampleLoading(true);
    try {
      const s = await api(`/vendor/samples/${key}/load`, { method: "POST" });
      setForm(merge(EMPTY, s.form));
      setDocs(s.documents);
    } catch (e) { setErr(e); } finally { setBusy(false); setSampleLoading(false); }
  };

  const submit = async () => {
    setErr(null); setBusy(true);
    try {
      const body: any = { operation_id: op.current, form: clean(form), vendor_note: note || null,
        documents: Object.fromEntries(Object.entries(docs).filter(([, v]) => v).map(([k, v]) => [k, v!.document_id])) };
      body.form.declarations.information_correct = !!form.declarations.information_correct;
      // A value hidden by an answer ("not GST registered", "no conflict") is not submitted: it would otherwise be
      // checked although the vendor can no longer see or correct it. It stays in the form if they switch back.
      if (form.tax.gst_registered !== "yes") body.form.tax.gstin = null;
      if (form.tax.msme_registered !== "yes") body.form.tax.udyam_number = null;
      if (form.declarations.conflict_of_interest !== "disclosed") body.form.declarations.conflict_details = null;
      if (correction) body.based_on_revision = basedOnRevision;
      const r = await api("/vendor/submit", { body });
      onSubmitted(r);
    } catch (e: any) {
      setErr(e);
      // Keep the same operation id: if the request did reach the server, a retry replays it instead of
      // creating a second revision. Only a server that says the id was used for different content needs a new one.
      if (e.code === "operation_reused") op.current = opId();
    } finally { setBusy(false); }
  };

  const docRows = useMemo(() => {
    if (!cfg) return [];
    return cfg.policy.documents.map((d: any) => {
      let applies: boolean | null = true;
      if (d.when) {
        const v = get(form, d.when.field);
        applies = v === "" || v == null ? null : d.when.op === "eq" ? v === d.when.value : d.when.op === "ne" ? v !== d.when.value : d.when.value.includes(v);
      }
      return { ...d, applies, label: cfg.document_types[d.document_type], trigger: d.when ? cfg.field_labels[d.when.field] : null };
    });
  }, [cfg, form]);

  if (!cfg && cfgErr) return (
    <Card title="Couldn't load the form">
      <div className="space-y-3"><ErrorNote error={cfgErr} /><Button variant="secondary" onClick={reloadCfg}><RefreshCw className="h-4 w-4" />Try again</Button></div>
    </Card>
  );
  if (!cfg) return <Card><div className="flex items-center gap-2 text-sm text-slate-500"><Loader2 className="h-4 w-4 animate-spin" /> Loading form…</div></Card>;
  const hi = (k: string) => highlight.some((h) => h.includes(k));

  return (
    <div className="space-y-5">
      {!correction && (
        <div className="flex flex-wrap items-center gap-3 rounded-xl border border-dashed border-brand-200 bg-brand-50/50 px-4 py-3">
          <FlaskConical className="h-4 w-4 text-brand-600" />
          <span className="text-sm text-slate-700">Demo helper — prefill with a prepared synthetic vendor (real PDFs; read by {cfg.reader?.mode === "fixture"
            ? <b className="text-amber-800">the labelled test reader, not a live model</b> : <>the live model: {cfg.reader?.describe}</>}):</span>
          <select className={clsx(inputCls, "w-full min-w-0 sm:w-auto sm:min-w-72")} defaultValue="" onChange={(e) => loadSample(e.target.value)} disabled={busy || uploading.size > 0}>
            <option value="">Choose a sample…</option>
            {samples?.map((s) => <option key={s.key} value={s.key}>{s.title}</option>)}
          </select>
          {sampleLoading && <span role="status" className="flex items-center gap-1.5 text-sm text-slate-600"><Loader2 className="h-4 w-4 animate-spin" />Loading the sample's form and documents…</span>}
        </div>
      )}
      {correction && (
        <div className="rounded-xl border border-sky-200 bg-sky-50 px-4 py-3 text-sm text-sky-900">
          You are correcting <b>revision {basedOnRevision}</b>. Change only what's needed — unchanged documents stay attached. Submitting creates
          revision {basedOnRevision! + 1}; the earlier version is kept in the history.
          {samples && (
            <span className="mt-2 flex flex-wrap items-center gap-2 text-xs">
              <FlaskConical className="h-3.5 w-3.5" /> Demo: replace everything with a prepared revision
              <select className={clsx(inputCls, "w-auto py-1 text-xs")} defaultValue="" onChange={(e) => loadSample(e.target.value)} disabled={busy || uploading.size > 0}>
                <option value="">Choose…</option>
                {samples.map((s) => <option key={s.key} value={s.key}>{s.title}</option>)}
              </select>
            </span>
          )}
        </div>
      )}

      {/* Locked while a sample loads or the form submits: a late sample reply would silently replace edits made meanwhile. */}
      <fieldset disabled={busy} className="min-w-0 space-y-5 disabled:opacity-70">
      <Section n={1} title="Business identity" hi={hi("registration") || hi("gst_identity") || hi("form_values")}>
        <Field label="Legal name (as registered)" required><input className={inputCls} {...f("business.legal_name")} /></Field>
        <Field label="Trade name" hint="If you trade or bank under another name"><input className={inputCls} {...f("business.trade_name")} /></Field>
        <Field label="Entity type" required>
          <select className={inputCls} {...f("business.entity_type")}>
            <option value="">Select…</option><option value="private_limited">Private limited company</option><option value="llp">Limited liability partnership (LLP)</option><option value="other">Other</option>
          </select>
        </Field>
        <Field label="CIN / LLPIN" required><input className={clsx(inputCls, "mono")} {...f("business.registration_number")} placeholder="U72200KA2019PTC128734 or AAB-1234" /></Field>
        <Field label="Date of incorporation"><input className={inputCls} {...f("business.incorporation_date")} placeholder="DD/MM/YYYY" /></Field>
        <div />
        <Field label="Registered office address" required><input className={inputCls} {...f("business.registered_address.line1")} /></Field>
        <div className="grid grid-cols-3 gap-3">
          <Field label="City" required><input className={inputCls} {...f("business.registered_address.city")} /></Field>
          <Field label="State" required>
            <select className={inputCls} {...f("business.registered_address.state")}><option value="">—</option>{cfg.states.map((s: string) => <option key={s}>{s}</option>)}</select>
          </Field>
          <Field label="PIN" required><input className={clsx(inputCls, "mono")} {...f("business.registered_address.pincode")} /></Field>
        </div>
      </Section>

      <Section n={2} title="Services offered">
        <Field label="Service category" required>
          <select className={inputCls} {...f("service.category")}><option value="">Select…</option>{cfg.service_categories.map((c: string) => <option key={c}>{c}</option>)}</select>
        </Field>
        <Question label="Will you work on our premises?" required><YesNo {...f("service.onsite_work")} /></Question>
        <Field label="Short description"><input className={inputCls} {...f("service.description")} /></Field>
        <Field label="Buyer contact / department"><input className={inputCls} {...f("service.buyer_contact")} /></Field>
      </Section>

      <Section n={3} title="Contact and signatory" hi={hi("form_values")}>
        <Field label="Contact name" required><input className={inputCls} {...f("contact.name")} /></Field>
        <Field label="Role"><input className={inputCls} {...f("contact.role")} /></Field>
        <Field label="Email" required><input className={inputCls} {...f("contact.email")} /></Field>
        <Field label="Phone"><input className={inputCls} {...f("contact.phone")} /></Field>
        <Field label="Authorised signatory" required><input className={inputCls} {...f("contact.signatory_name")} /></Field>
        <Field label="Signatory designation"><input className={inputCls} {...f("contact.signatory_designation")} /></Field>
      </Section>

      <Section n={4} title="Tax and classifications" hi={hi("pan") || hi("gstin") || hi("msme")}>
        <Field label="Business PAN" required hint="The company's / LLP's own PAN — not a director's"><input className={clsx(inputCls, "mono uppercase")} {...f("tax.pan")} /></Field>
        <Question label="GST registered?" required><YesNo {...f("tax.gst_registered")} /></Question>
        {form.tax.gst_registered === "yes" && <Field label="GSTIN" required><input className={clsx(inputCls, "mono uppercase")} {...f("tax.gstin")} /></Field>}
        <Question label="MSME (Udyam) registered?" required><YesNo {...f("tax.msme_registered")} /></Question>
        {form.tax.msme_registered === "yes" && <Field label="Udyam registration number" required><input className={clsx(inputCls, "mono uppercase")} {...f("tax.udyam_number")} placeholder="UDYAM-KA-03-0045127" /></Field>}
      </Section>

      <Section n={5} title="Banking" hi={hi("bank")}>
        <Field label="Account holder name" required><input className={inputCls} {...f("bank.account_holder")} /></Field>
        <Field label="Account number" required><input className={clsx(inputCls, "mono")} {...f("bank.account_number")} /></Field>
        <Field label="IFSC" required><input className={clsx(inputCls, "mono uppercase")} {...f("bank.ifsc")} /></Field>
        <Field label="Bank name" required><input className={inputCls} {...f("bank.bank_name")} /></Field>
        <Field label="Branch"><input className={inputCls} {...f("bank.branch")} /></Field>
      </Section>

      <Section n={6} title="Declarations" hi={hi("declarations")}>
        <div className="sm:col-span-2 space-y-3 text-sm">
          <label className="flex items-start gap-2">
            <input type="checkbox" className="mt-1" checked={!!form.declarations.information_correct}
              onChange={(e) => setForm(setPath(form, "declarations.information_correct", e.target.checked))} />
            <span>I confirm the information is correct and I will notify the buyer of any change, especially to bank details. <span className="text-red-500">*</span></span>
          </label>
          <Question label="Do you accept the buyer's supplier code of conduct?" required
            after={form.declarations.conduct_acceptance && <button type="button" className="text-xs text-slate-500 underline" onClick={() => setForm(setPath(form, "declarations.conduct_acceptance", ""))}>clear answer</button>}>
            {[["accepted", "I accept"], ["declined", "I do not accept"]].map(([v, l]) => (
              <Choice key={v} active={form.declarations.conduct_acceptance === v} onClick={() => setForm(setPath(form, "declarations.conduct_acceptance", v))}>{l}</Choice>
            ))}
          </Question>
          <div>
            <Question label="Any conflict of interest (e.g. a relative employed by the buyer)?">
              {[["none", "None"], ["disclosed", "Yes — I'll describe it"]].map(([v, l]) => (
                <Choice key={v} active={form.declarations.conflict_of_interest === v} onClick={() => setForm(setPath(form, "declarations.conflict_of_interest", v))}>{l}</Choice>
              ))}
            </Question>
            {form.declarations.conflict_of_interest === "disclosed" && <input className={clsx(inputCls, "mt-2")} placeholder="Describe the relationship" {...f("declarations.conflict_details")} />}
          </div>
        </div>
      </Section>
      </fieldset>

      <Card title={<span className="flex items-center gap-2"><Paperclip className="h-3.5 w-3.5" />Documents</span>}>
        <p className="mb-4 text-sm text-slate-600">What this buyer needs depends on your answers above. PDF only, up to {cfg.limits.max_file_mb} MB and {cfg.limits.max_pages_per_file} pages each.</p>
        <div className="divide-y divide-slate-100 rounded-lg border border-slate-200">
          {docRows.map((d: any) => (
            <DocRow key={d.id} row={d} value={docs[d.document_type]} highlight={hi(d.document_type)}
              onChange={(v) => setDocs((prev: any) => ({ ...prev, [d.document_type]: v }))}
              locked={busy}
              onBusy={(b) => setUploading((prev) => { const n = new Set(prev); if (b) n.add(d.document_type); else n.delete(d.document_type); return n; })} />
          ))}
        </div>
      </Card>

      {correction && (
        <Card title="Note to the reviewer (optional)">
          <textarea className={clsx(inputCls, "min-h-20 disabled:opacity-70")} value={note} onChange={(e) => setNote(e.target.value)} disabled={busy} placeholder="e.g. We moved to a new bank account in October." />
        </Card>
      )}

      <div className="sticky bottom-4 z-10 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-slate-200 bg-white/95 px-4 py-3 shadow-lg backdrop-blur">
        <div className="min-w-0 flex-1"><ErrorNote error={err} /></div>
        <div className="flex gap-2">
          {/* Locked while submitting, like the fields: the submission can't be called back, so neither an edit nor a Cancel
              may look like it changed or stopped it (and a late reply must not land on a draft opened after a Cancel). */}
          {onCancel && <Button variant="secondary" onClick={onCancel} disabled={busy}>Cancel</Button>}
          {uploading.size > 0 && <span className="self-center text-xs text-slate-500">Waiting for {uploading.size} upload(s)…</span>}
          <Button onClick={submit} disabled={busy || uploading.size > 0}>{busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}{correction ? `Submit revision ${basedOnRevision! + 1}` : "Submit application"}</Button>
        </div>
      </div>
    </div>
  );
}

function Section({ n, title, children, hi }: { n: number; title: string; children: React.ReactNode; hi?: boolean }) {
  return (
    <Card className={clsx(hi && "ring-2 ring-amber-300")} title={<span className="flex items-center gap-2"><span className="flex h-5 w-5 items-center justify-center rounded-full bg-slate-100 text-[11px] text-slate-600">{n}</span>{title}{hi && <span className="rounded bg-amber-100 px-1.5 text-[10px] text-amber-900">needs your attention</span>}</span>}>
      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">{children}</div>
    </Card>
  );
}

/** A question answered by choosing one option. Not a <label>: a label around several buttons gives them all the
 *  question's name (screen readers announced "Yes" as "GST registered? No"). The options are toggle buttons in a group
 *  named by the question, and each says whether it is pressed (the chosen one); Tab, Enter and Space work as usual. */
function Question({ label, required, children, after }: { label: string; required?: boolean; children: React.ReactNode; after?: React.ReactNode }) {
  const id = useId();
  return (
    <div>
      <span id={id} className="mb-1 block text-[13px] font-semibold text-slate-700">{label}{required && <span className="text-red-600" aria-hidden="true"> *</span>}</span>
      <div className="flex flex-wrap items-center gap-2">
        <div role="group" aria-labelledby={id} className="flex flex-wrap gap-2">{children}</div>
        {after}
      </div>
    </div>
  );
}

function YesNo({ value, onChange }: { value: string; onChange: (e: any) => void }) {
  return <>{["yes", "no"].map((v) => <Choice key={v} active={value === v} onClick={() => onChange({ target: { value: v } })}>{v === "yes" ? "Yes" : "No"}</Choice>)}</>;
}

function Choice({ active, onClick, children }: { active: boolean; onClick: () => void; children: React.ReactNode }) {
  return (
    <button type="button" aria-pressed={active} onClick={onClick} className={clsx("rounded-lg border px-3 py-1.5 text-sm transition-colors",
      active ? "border-brand-500 bg-brand-50 font-medium text-brand-700" : "border-slate-300 bg-white text-slate-700 hover:bg-slate-50")}>{children}</button>
  );
}

function DocRow({ row, value, onChange, onBusy, highlight, locked }: { row: any; value?: any; onChange: (v: any) => void; onBusy: (b: boolean) => void; highlight: boolean; locked: boolean }) {
  const [busy, setBusyState] = useState(false);
  const setBusy = (b: boolean) => { inFlight.current = b; setBusyState(b); onBusy(b); };
  const inFlight = useRef(false); // one upload per slot at a time: a second Replace can't race the first
  const [err, setErr] = useState<string | null>(null);
  const input = useRef<HTMLInputElement>(null);
  const upload = async (file?: File) => {
    if (!file || inFlight.current) return;
    setBusy(true); setErr(null);
    try {
      const fd = new FormData(); fd.append("file", file);
      onChange(await api("/vendor/documents", { form: fd }));
    } catch (e: any) { setErr(e.message); } finally { setBusy(false); if (input.current) input.current.value = ""; }
  };
  const need = row.applies === false ? "Not needed" : row.applies === null ? `Answer "${row.trigger}" first` :
    row.level === "required" ? (row.when ? `Required because ${row.trigger} = ${row.when.value}` : "Required") : "Optional";
  return (
    <div className={clsx("flex flex-wrap items-center gap-3 px-4 py-3", highlight && "bg-amber-50", row.applies === false && "opacity-60")}>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium text-slate-800">{row.label}</span>
          <span className={clsx("rounded px-1.5 py-0.5 text-[11px] font-semibold",
            row.applies === false ? "bg-slate-100 text-slate-500" : row.level === "required" && row.applies ? "bg-red-50 text-red-700" : "bg-slate-100 text-slate-600")}>{need}</span>
        </div>
        <p className="text-xs text-slate-500">{row.reason}</p>
        {err && <p className="mt-1 text-xs font-medium text-red-700">{err}</p>}
      </div>
      {value ? (
        <div className="flex min-w-0 max-w-full items-center gap-2 rounded-lg bg-emerald-50 px-2.5 py-1.5 text-xs text-emerald-900">
          <CheckCircle2 className="h-3.5 w-3.5 shrink-0" />
          <span className="min-w-0 truncate font-medium sm:max-w-56" title={value.filename}>{value.filename}</span>
          <span className="shrink-0 text-emerald-700">· {value.pages}p · {layoutLabel(value.layout)}</span>
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <>
            <button onClick={() => input.current?.click()} disabled={locked} title="Replace" className="rounded p-0.5 hover:bg-emerald-100 disabled:opacity-40"><RefreshCw className="h-3.5 w-3.5" /></button>
            <button onClick={() => onChange(undefined)} disabled={locked} title="Remove" className="rounded p-0.5 hover:bg-emerald-100 disabled:opacity-40"><X className="h-3.5 w-3.5" /></button></>}
        </div>
      ) : (
        <Button variant="secondary" size="sm" onClick={() => input.current?.click()} disabled={busy || locked}>
          {busy ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <FileUp className="h-3.5 w-3.5" />} Upload PDF
        </Button>
      )}
      <input ref={input} type="file" accept="application/pdf,.pdf" className="hidden" onChange={(e) => upload(e.target.files?.[0])} />
    </div>
  );
}

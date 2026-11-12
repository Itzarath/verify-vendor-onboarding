import clsx from "clsx";
import { ArrowRight, Building2, UserCheck } from "lucide-react";
import { useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useAuth } from "../App";
import { Button, ErrorNote, Field, inputCls, LoadFailed, SealMark } from "../components/ui";
import { api } from "../lib/api";
import { useResource } from "../lib/hooks";

const STAGES: [string, string][] = [
  ["Receive", "The application and its PDFs arrive as one revision."],
  ["Read", "Each document is read on its own, without seeing the form."],
  ["Compare", "Form against documents, and against the last version."],
  ["Check", "The buyer's policy, Indian ID rules and the GST registry."],
  ["Decide", "Ready for approval, waiting on someone, or refused by rule."],
  ["Communicate", "The supplier is told exactly what is still needed."],
];

export default function Login() {
  const { data, error: loadErr, reload } = useResource<any>("/auth/personas");
  const { setMe } = useAuth();
  const nav = useNavigate();
  const [tab, setTab] = useState<"supplier" | "buyer">("supplier");
  const [company, setCompany] = useState("");
  const [buyer, setBuyer] = useState("meridian");
  const [err, setErr] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [signingIn, setSigningIn] = useState<number | null>(null);
  const [loginErr, setLoginErr] = useState<any>(null);

  // One sign-in at a time, however it is started (a click, Enter, a new account or an existing one): set at once, so a
  // second action can't start while the first is pending, and a slow first one can't replace a later choice.
  const pending = useRef(false);
  const login = async (user_id: number) => {
    if (pending.current) return;
    pending.current = true;
    setSigningIn(user_id); setLoginErr(null);
    try {
      const me = await api("/auth/login", { body: { user_id } });
      setMe(me);
      nav(me.role === "vendor" ? "/vendor" : "/review");
    } catch (e) { setLoginErr(e); } finally { pending.current = false; setSigningIn(null); }
  };
  // Without the account list there is nothing to sign in with: say so, and offer a retry.
  const listFailed = !data && loadErr && <LoadFailed what="the demo accounts" error={loadErr} onRetry={reload} />;
  const canStart = !!data && company.trim().length >= 3;
  const start = async () => {
    if (pending.current || !canStart) return; // the same rule as the button, for every way of starting
    pending.current = true;
    setBusy(true); setErr(null);
    try {
      const me = await api("/auth/new-vendor", { body: { company_name: company, buyer } });
      setMe(me); nav("/vendor");
    } catch (e) { setErr(e); } finally { pending.current = false; setBusy(false); }
  };

  return (
    <div className="min-h-screen lg:grid lg:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
      {/* The desk: what Verify does, in the order it does it. */}
      <section className="guilloche relative overflow-hidden bg-ink px-6 py-10 text-white sm:px-10 lg:min-h-screen lg:px-14 lg:py-14">
        <div className="flex items-center gap-3">
          <SealMark light className="h-10 w-10" />
          <span className="display text-2xl font-bold">Verify</span>
        </div>
        <h1 className="display mt-12 max-w-xl text-[2.6rem] font-bold leading-[1.02] sm:text-[3.4rem]">
          Know who you’re paying before the first invoice.
        </h1>
        <p className="mt-5 max-w-lg text-[17px] leading-relaxed text-white/75">
          Indian companies and LLPs apply with their PAN, GST, incorporation and bank documents. Verify reads every file,
          checks it against the form and the registry, and tells the supplier precisely what is still missing.
        </p>
        <ol className="mt-10 max-w-lg border-l border-white/15">
          {STAGES.map(([name, text], i) => (
            <li key={name} className="rise relative pb-4 pl-7 last:pb-0" style={{ animationDelay: `${120 + i * 90}ms` }}>
              <span className="absolute -left-[13px] top-0 flex h-[25px] w-[25px] items-center justify-center rounded-full border border-white/25 bg-ink text-xs font-semibold text-white/80">{i + 1}</span>
              <span className="display block text-[17px] font-semibold leading-6">{name}</span>
              <span className="block text-sm text-white/60">{text}</span>
            </li>
          ))}
        </ol>
        <p className="mt-10 max-w-lg text-sm text-white/50">
          A case-study demo. The companies, people and documents are fictional, and GST registry results are simulated and labelled as such.
        </p>
      </section>

      {/* Sign in */}
      <section className="flex items-start justify-center px-4 py-10 sm:px-8 lg:items-center lg:py-14">
        <div className="w-full max-w-md">
          <h2 className="text-2xl font-bold text-slate-900">Sign in to the demo</h2>
          <p className="mt-1 text-[15px] text-slate-600">Pick a side of the process. You can switch at any time.</p>

          <div role="tablist" className="mt-6 grid grid-cols-2 rounded-lg border border-slate-200 bg-white p-1">
            {([["supplier", "Apply as a supplier", Building2], ["buyer", "Review applications", UserCheck]] as const).map(([k, l, I]) => (
              <button key={k} role="tab" aria-selected={tab === k} onClick={() => setTab(k)}
                className={clsx("flex items-center justify-center gap-2 rounded-md px-3 py-2 text-sm font-semibold transition-colors",
                  tab === k ? "bg-ink text-white" : "text-slate-600 hover:bg-slate-100")}>
                <I className="h-4 w-4" />{l}
              </button>
            ))}
          </div>

          {tab === "supplier" ? (
            <div className="mt-5 space-y-5">
              <div className="space-y-3 rounded-lg border border-slate-200 bg-white p-5">
                <Field label="Your company">
                  <input className={inputCls} placeholder="e.g. Nimbus Ledger Solutions Private Limited" value={company}
                    onChange={(e) => setCompany(e.target.value)} onKeyDown={(e) => e.key === "Enter" && start()} />
                </Field>
                <Field label="Applying to">
                  <select className={inputCls} value={buyer} onChange={(e) => setBuyer(e.target.value)}>
                    {data?.buyers.map((b: any) => <option key={b.slug} value={b.slug}>{b.name}</option>)}
                  </select>
                </Field>
                <Button onClick={start} disabled={busy || signingIn !== null || !canStart} className="w-full">Start an application <ArrowRight className="h-4 w-4" /></Button>
                <ErrorNote error={err} />
              </div>
              {listFailed}
              <ErrorNote error={loginErr} />
              {!!data?.vendors.length && (
                <div>
                  <h3 className="label mb-2">Or continue an application</h3>
                  <ul className="max-h-60 divide-y divide-slate-200 overflow-y-auto rounded-lg border border-slate-200 bg-white">
                    {data.vendors.map((v: any) => (
                      <li key={v.id}>
                        <button onClick={() => login(v.id)} disabled={busy || signingIn !== null} className="flex w-full items-center justify-between gap-3 px-4 py-2.5 text-left transition-colors hover:bg-slate-50">
                          <span className="min-w-0">
                            <span className="block truncate text-sm font-semibold text-slate-800">{v.name}</span>
                            <span className="block truncate text-xs text-slate-500">
                              {v.reference}{v.signed_up_as ? ` · signed up as ${v.signed_up_as}` : ""}
                              {v.application ? ` · ${v.application}, ${String(v.state).replace(/_/g, " ").toLowerCase()}` : " · not submitted yet"}
                            </span>
                          </span>
                          <span className="shrink-0 text-xs text-slate-500">{v.buyer.replace(" (fictional)", "")}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </div>
          ) : (
            <div className="mt-5">
              <p className="mb-3 text-sm text-slate-600">Procurement reviewers decide on applications. Finance reviewers clear changes to bank details.</p>
              {listFailed}
              <div className="mb-3"><ErrorNote error={loginErr} /></div>
              <ul className="divide-y divide-slate-200 rounded-lg border border-slate-200 bg-white">
                {data?.staff.map((s: any) => (
                  <li key={s.id}>
                    <button onClick={() => login(s.id)} disabled={busy || signingIn !== null} className="group flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-slate-50">
                      <span className={clsx("flex h-9 w-9 shrink-0 items-center justify-center rounded-full text-sm font-bold",
                        s.role === "finance" ? "bg-teal-100 text-teal-900" : "bg-violet-100 text-violet-900")}>
                        {s.name.split(" ").map((p: string) => p[0]).slice(0, 2).join("")}
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="block text-sm font-semibold text-slate-900">{s.name.split(" — ")[0]}</span>
                        <span className="block truncate text-xs text-slate-500">{s.role === "finance" ? "Finance reviewer" : "Procurement reviewer"}, {s.buyer.replace(" (fictional)", "")}</span>
                      </span>
                      <ArrowRight className="h-4 w-4 text-slate-300 transition-transform group-hover:translate-x-0.5 group-hover:text-slate-500" />
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      </section>
    </div>
  );
}

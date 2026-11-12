import { KeyRound } from "lucide-react";
import { useState } from "react";
import { Button, inputCls, SealMark } from "../components/ui";
import { api } from "../lib/api";

/** Shown before anything else on the hosted demo: one shared access code for evaluators. */
export default function AccessGate({ onGranted }: { onGranted: () => void }) {
  const [code, setCode] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true); setErr(null);
    try {
      await api("/access", { body: { code } });
      onGranted();
    } catch (x: any) {
      setErr(x.message || "That access code is not right. Check it and try again.");
    } finally { setBusy(false); }
  };
  return (
    <div className="guilloche flex min-h-screen items-center justify-center bg-ink px-4 py-10">
      <form onSubmit={submit} className="w-full max-w-sm rounded-lg bg-paper p-7 shadow-[0_20px_60px_-20px_rgba(0,0,0,.6)]">
        <div className="flex items-center gap-3">
          <SealMark className="h-10 w-10" />
          <div>
            <div className="display text-xl font-bold text-slate-900">Verify</div>
            <div className="text-sm text-slate-500">Supplier onboarding, case-study demo</div>
          </div>
        </div>
        <p className="mt-6 text-[15px] text-slate-700">This demo is shared with evaluators. Enter the access code you were given to continue.</p>
        <label className="mt-4 mb-1 block text-[13px] font-semibold text-slate-700" htmlFor="access-code">Access code</label>
        <input id="access-code" type="password" autoComplete="off" autoFocus className={inputCls} value={code}
          onChange={(e) => setCode(e.target.value)} />
        {err && <p role="alert" className="mt-2 text-sm font-medium text-red-700">{err}</p>}
        <Button type="submit" className="mt-5 w-full" disabled={busy || !code.trim()}><KeyRound className="h-4 w-4" />Continue</Button>
        <p className="mt-5 text-xs text-slate-500">All companies, people and documents in this demo are fictional.</p>
      </form>
    </div>
  );
}

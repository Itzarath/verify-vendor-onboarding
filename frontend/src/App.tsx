import { LogOut, RefreshCw } from "lucide-react";
import { createContext, useContext, useEffect, useState } from "react";
import { Link, Navigate, Route, Routes, useNavigate } from "react-router-dom";
import { api, setSignedInAs, SIGNED_IN_ELSEWHERE } from "./lib/api";
import AccessGate from "./pages/AccessGate";
import CaseView from "./pages/CaseView";
import Login from "./pages/Login";
import ReviewDashboard from "./pages/ReviewDashboard";
import VendorHome from "./pages/VendorHome";
import { SealMark, Spinner } from "./components/ui";

export type Me = { id: number; name: string; role: "vendor" | "reviewer" | "finance"; buyer: { name: string; slug: string }; vendor?: { id: number; name: string } };

const AuthCtx = createContext<{ me: Me | null; setMe: (m: Me | null) => void; elsewhere: boolean }>({ me: null, setMe: () => {}, elsewhere: false });
export const useAuth = () => useContext(AuthCtx);

export default function App() {
  const [me, setMeState] = useState<Me | null>(null);
  // Set together with the account shown, so every request from here on says which account this tab is showing.
  const setMe = (m: Me | null) => { setSignedInAs(m?.id ?? null); setMeState(m); };
  // Set when the server refuses a request because this browser is now signed in as someone else (another tab).
  const [elsewhere, setElsewhere] = useState(false);
  useEffect(() => {
    const on = () => setElsewhere(true);
    window.addEventListener(SIGNED_IN_ELSEWHERE, on);
    return () => window.removeEventListener(SIGNED_IN_ELSEWHERE, on);
  }, []);
  const [ready, setReady] = useState(false);
  const [needsCode, setNeedsCode] = useState(false);
  const load = () => {
    setReady(false);
    // the hosted demo sits behind a shared access code; ask for it before anything else
    api<{ required: boolean; granted: boolean }>("/access")
      .then((a) => {
        if (a.required && !a.granted) { setNeedsCode(true); setReady(true); return; }
        setNeedsCode(false);
        return api<Me>("/auth/me").then(setMe).catch(() => setMe(null)).finally(() => setReady(true));
      })
      .catch(() => setReady(true));
  };
  useEffect(load, []);
  if (!ready) return <div className="flex h-screen items-center justify-center"><Spinner className="h-6 w-6 text-slate-400" /></div>;
  if (needsCode) return <AccessGate onGranted={load} />;
  const home = !me ? "/" : me.role === "vendor" ? "/vendor" : "/review";
  return (
    <AuthCtx.Provider value={{ me, setMe, elsewhere }}>
      <Routes>
        <Route path="/" element={me ? <Navigate to={home} replace /> : <Login />} />
        <Route path="/vendor" element={me?.role === "vendor" ? <Shell><VendorHome /></Shell> : <Navigate to="/" replace />} />
        <Route path="/review" element={me && me.role !== "vendor" ? <Shell><ReviewDashboard /></Shell> : <Navigate to="/" replace />} />
        <Route path="/review/requests/:id" element={me && me.role !== "vendor" ? <Shell><CaseView /></Shell> : <Navigate to="/" replace />} />
        <Route path="*" element={<Navigate to={home} replace />} />
      </Routes>
    </AuthCtx.Provider>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  const { me, setMe, elsewhere } = useAuth();
  const nav = useNavigate();
  const [signOutErr, setSignOutErr] = useState<string | null>(null);
  const signOut = async () => {
    setSignOutErr(null);
    try { await api("/auth/logout", { method: "POST" }); setMe(null); nav("/"); }
    catch (e: any) { setSignOutErr(`Couldn't sign out: ${e?.message || e}. Please try again.`); }
  };
  const roleLabel = me?.role === "vendor" ? "Supplier" : me?.role === "finance" ? "Finance reviewer" : "Procurement reviewer";
  return (
    <div className="min-h-screen">
      <header className="guilloche sticky top-0 z-20 bg-ink text-white shadow-[0_1px_0_rgba(0,0,0,.2)]">
        <div className="mx-auto flex h-15 max-w-7xl items-center justify-between gap-4 px-4 sm:px-6">
          <Link to={me?.role === "vendor" ? "/vendor" : "/review"} className="flex min-w-0 items-center gap-3">
            <SealMark light className="h-8 w-8 shrink-0" />
            <span className="min-w-0 leading-tight">
              <span className="display block text-lg font-bold tracking-tight">Verify</span>
              <span className="block truncate text-xs text-white/65">{me?.role === "vendor" ? `Applying to ${me?.buyer.name}` : me?.buyer.name}</span>
            </span>
          </Link>
          <div className="flex items-center gap-2 sm:gap-4">
            <span className="hidden rounded-full border border-amber-300/50 px-2.5 py-0.5 text-xs font-medium text-amber-200 md:inline">Demo with fictional companies</span>
            <span className="hidden text-right leading-tight sm:block"><span className="block text-sm font-semibold">{me?.name.split(" — ")[0]}</span><span className="text-xs text-white/60">{roleLabel}</span></span>
            <button onClick={signOut} className="flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-sm text-white/75 transition-colors hover:bg-white/10 hover:text-white" title="Sign out">
              <LogOut className="h-4 w-4" /><span className="hidden sm:inline">Sign out</span>
            </button>
          </div>
        </div>
      </header>
      {elsewhere && (
        <div role="alert" className="mx-auto max-w-7xl px-4 pt-4 sm:px-6">
          <div className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-amber-300 bg-amber-50 px-3 py-2.5 text-sm text-amber-900">
            <span>You signed in as a different account in another tab. This tab still shows <b>{me?.name.split(" — ")[0]}</b> and can't save anything.</span>
            <button onClick={() => window.location.reload()} className="flex items-center gap-1.5 rounded-md bg-amber-900 px-2.5 py-1 text-xs font-medium text-white hover:bg-amber-800"><RefreshCw className="h-3.5 w-3.5" />Reload as that account</button>
          </div>
        </div>
      )}
      {signOutErr && (
        <div role="alert" className="mx-auto max-w-7xl px-4 pt-4 sm:px-6">
          <p className="rounded-md border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-800">{signOutErr}</p>
        </div>
      )}
      <main className="mx-auto max-w-7xl px-4 py-7 sm:px-6">{children}</main>
    </div>
  );
}

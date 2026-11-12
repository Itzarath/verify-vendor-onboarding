import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";

/** Fetch a resource; optionally keep polling while `pollMs` is set.
 *
 * Only a response for the CURRENT path is ever shown: when the path changes (e.g. browser history jumps from one
 * case to another), a late reply for the previous path is dropped and the old data is cleared, so a page never
 * shows one record's data under another record's URL. */
export function useResource<T = any>(path: string | null, pollMs?: number | null) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<Error | null>(null);
  const [loading, setLoading] = useState(true);
  const current = useRef<string | null>(path);

  const load = useCallback(async () => {
    if (!path) return;
    try {
      const d = await api<T>(path);
      if (current.current === path) { setData(d); setError(null); }
    } catch (e: any) {
      if (current.current === path) setError(e);
    } finally {
      if (current.current === path) setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    current.current = path;
    setData(null); setError(null); setLoading(true);
    load();
    return () => { current.current = null; };
  }, [load, path]);

  useEffect(() => {
    if (!pollMs) return;
    const t = setInterval(load, pollMs);
    return () => clearInterval(t);
  }, [load, pollMs]);

  return { data, error, loading, reload: load, setData };
}

export function useNow(active: boolean, ms = 1000) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(t);
  }, [active, ms]);
  return now;
}

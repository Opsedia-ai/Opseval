import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "./api";

export interface Resource<T> { data?: T; error?: ApiError; loading: boolean; reload: () => void }

/** Fetch on mount and when deps change; optional polling while `poll(data)` returns ms. */
export function useApi<T>(fn: () => Promise<T>, deps: unknown[] = [], poll?: (d: T) => number | false): Resource<T> {
  const [data, setData] = useState<T>();
  const [error, setError] = useState<ApiError>();
  const [loading, setLoading] = useState(true);
  const [tick, setTick] = useState(0);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const pollRef = useRef(poll);
  pollRef.current = poll;

  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    setLoading(true);
    fnRef.current().then(
      (d) => {
        if (!live) return;
        setData(d); setError(undefined); setLoading(false);
        const ms = pollRef.current?.(d);
        if (ms) timer = setTimeout(() => setTick((t) => t + 1), ms);
      },
      (e) => { if (live) { setError(e as ApiError); setLoading(false); } },
    );
    return () => { live = false; if (timer) clearTimeout(timer); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);

  return { data, error, loading: loading && !data, reload: useCallback(() => setTick((t) => t + 1), []) };
}

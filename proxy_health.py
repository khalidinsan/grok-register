"""
Proxy health check before farm starts.

Flow:
  1. Load proxy list.
  2. Decide how many GOOD proxies we need:
       - finite farm  N accounts  → need N  (10 akun → stop after 10 good)
       - unlimited               → need ~ concurrent*3 (working pool)
  3. Probe in parallel batches against accounts.x.ai until:
       - kept >= need, OR
       - list exhausted
  4. Do NOT scan all 100 if 10 good already found.
  5. Sort kept fast→slow; pass to workers.

HTTP 403 on accounts.x.ai without cookies is OK (tunnel works).
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Sequence

import requests

from proxy_util import mask_proxy, normalize_proxy, parse_proxy

DEFAULT_CHECK_URL = "https://accounts.x.ai/"
# Secondary hop used during xAI signup cookie chain (often fails on bad tunnels)
SECONDARY_CHECK_URL = "https://auth.grokipedia.com/"
DEFAULT_MAX_MS = 4000.0
DEFAULT_TIMEOUT_SEC = 12.0
DEFAULT_WORKERS = 10


@dataclass
class ProxyProbeResult:
    proxy: str
    ok: bool
    ms: float
    status: int = 0
    error: str = ""
    port: Optional[int] = None
    host: str = ""

    @property
    def label(self) -> str:
        if self.port:
            return f":{self.port}"
        return mask_proxy(self.proxy)[:48]


@dataclass
class ProxyFilterReport:
    target: str
    max_ms: float
    need: int = 0
    checked: List[ProxyProbeResult] = field(default_factory=list)
    kept: List[str] = field(default_factory=list)
    dropped: List[ProxyProbeResult] = field(default_factory=list)
    skipped_unchecked: int = 0
    elapsed_ms: float = 0.0
    early_stop: bool = False

    @property
    def kept_count(self) -> int:
        return len(self.kept)

    @property
    def drop_count(self) -> int:
        return len(self.dropped)

    @property
    def total_checked(self) -> int:
        return len(self.checked)


def resolve_proxy_need(
    *,
    total_accounts: int,
    concurrent: int = 1,
    proxy_count: int,
    proxy_mode: str = "per_account",
    explicit_need: Optional[int] = None,
) -> int:
    """
    How many good proxies to collect before stopping the check.

    - explicit_need set → use that (capped by proxy_count)
    - finite N accounts + per_account → need N (1 proxy per account ideal)
    - finite N + per_worker → need concurrent (sticky per worker)
    - unlimited (0) → modest working set: max(concurrent*3, concurrent+2)
    """
    n = max(0, int(proxy_count))
    if n == 0:
        return 0
    if explicit_need is not None and int(explicit_need) > 0:
        return min(int(explicit_need), n)

    mode = (proxy_mode or "per_account").strip().lower()
    conc = max(1, int(concurrent or 1))
    total = int(total_accounts or 0)

    if total > 0:
        if mode in ("per_worker", "worker", "sticky"):
            return min(conc, n)
        # per_account: one good proxy per planned account
        return min(total, n)

    # unlimited: enough for rotation without probing the whole list
    return min(max(conc * 3, conc + 2, 6), n)


def _http_get_via_proxy(
    proxy_url: str,
    target: str,
    timeout: float,
) -> tuple[bool, float, int, str]:
    """Returns (ok, ms, status, error)."""
    proxies = {"http": proxy_url, "https": proxy_url}
    t0 = time.perf_counter()
    try:
        r = requests.get(
            target,
            proxies=proxies,
            timeout=timeout,
            allow_redirects=True,
            stream=True,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; GrokFarmProxyCheck/1.0)",
                "Accept": "*/*",
            },
        )
        try:
            next(r.iter_content(256), None)
        except Exception:
            pass
        finally:
            r.close()
        ms = (time.perf_counter() - t0) * 1000.0
        return True, ms, int(r.status_code), ""
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000.0
        return False, ms, 0, f"{type(e).__name__}: {e}"


def probe_proxy(
    proxy_url: str,
    *,
    target: str = DEFAULT_CHECK_URL,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    secondary: Optional[str] = SECONDARY_CHECK_URL,
) -> ProxyProbeResult:
    """
    HTTPS GET through proxy; any HTTP response counts as tunnel OK.

    If `secondary` is set (default auth.grokipedia.com), also require that
    host to succeed — signup cookie chain fails with ERR_TUNNEL_CONNECTION_FAILED
    when only accounts.x.ai works but grokipedia tunnel dies.
    """
    raw = normalize_proxy(proxy_url) or (proxy_url or "").strip()
    info = parse_proxy(raw)
    url = info.get("url") or raw
    result = ProxyProbeResult(
        proxy=url,
        ok=False,
        ms=0.0,
        port=info.get("port"),
        host=info.get("host") or "",
    )
    if not url:
        result.error = "empty proxy"
        return result

    ok, ms, status, err = _http_get_via_proxy(url, target, timeout)
    result.ms = ms
    result.status = status
    if not ok:
        result.error = err
        result.ok = False
        return result

    # Primary OK — optional secondary domain (xAI auth hop)
    sec = (secondary or "").strip()
    if sec:
        ok2, ms2, status2, err2 = _http_get_via_proxy(url, sec, timeout)
        # count worst-case latency for threshold
        result.ms = max(ms, ms2)
        if not ok2:
            result.ok = False
            result.status = status2
            result.error = f"secondary tunnel fail ({sec}): {err2}"
            return result
        result.status = status  # keep primary status for logs

    result.ok = True
    return result


def _probe_batch(
    batch: Sequence[str],
    *,
    target: str,
    timeout: float,
    workers: int,
) -> List[ProxyProbeResult]:
    results: List[ProxyProbeResult] = []
    if not batch:
        return results
    n_workers = max(1, min(int(workers), len(batch)))
    with ThreadPoolExecutor(max_workers=n_workers) as ex:
        futs = {
            ex.submit(probe_proxy, p, target=target, timeout=timeout): p
            for p in batch
        }
        for fut in as_completed(futs):
            try:
                results.append(fut.result())
            except Exception as e:
                p = futs[fut]
                results.append(
                    ProxyProbeResult(proxy=p, ok=False, ms=0.0, error=str(e))
                )
    return results


def filter_proxies(
    proxies: Sequence[str],
    *,
    target: str = DEFAULT_CHECK_URL,
    max_ms: float = DEFAULT_MAX_MS,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    workers: int = DEFAULT_WORKERS,
    need: Optional[int] = None,
    log: Optional[Callable[[str], None]] = None,
) -> ProxyFilterReport:
    """
    Probe proxies in batches until `need` good ones found (or list ends).

    need=None → check entire list (legacy).
    need=10   → stop after 10 KEEP (skip rest unchecked).
    """
    def _log(msg: str) -> None:
        if log:
            log(msg)
        else:
            print(msg, flush=True)

    unique: List[str] = []
    seen = set()
    for p in proxies:
        n = normalize_proxy(str(p)) or str(p).strip()
        if not n or n in seen:
            continue
        seen.add(n)
        unique.append(n)

    report = ProxyFilterReport(target=target, max_ms=max_ms)
    if not unique:
        return report

    if need is None or need <= 0:
        need_n = len(unique)
    else:
        need_n = min(int(need), len(unique))
    report.need = need_n

    batch_size = max(1, min(int(workers), need_n, len(unique)))
    # slightly larger batch than remaining need to absorb duds in one round
    def _next_batch_size(remaining_need: int, left: int) -> int:
        # probe up to 2× remaining need (capped), so a few DROPs don't force many rounds
        want = max(remaining_need, min(left, max(remaining_need * 2, batch_size)))
        return max(1, min(want, left, max(int(workers), remaining_need)))

    _log(
        f"[PROXY-CHECK] need {need_n} good  of {len(unique)} listed  → {target}  "
        f"(max {max_ms:.0f}ms, timeout {timeout:.0f}s, parallel≤{workers})"
    )

    t0 = time.perf_counter()
    queue = list(unique)
    kept_rows: List[ProxyProbeResult] = []
    all_checked: List[ProxyProbeResult] = []
    dropped: List[ProxyProbeResult] = []
    round_i = 0

    while queue and len(kept_rows) < need_n:
        round_i += 1
        remaining_need = need_n - len(kept_rows)
        bsz = _next_batch_size(remaining_need, len(queue))
        batch = queue[:bsz]
        queue = queue[bsz:]

        _log(
            f"[PROXY-CHECK] round {round_i}: probe {len(batch)}  "
            f"(have {len(kept_rows)}/{need_n} good, {len(queue)} not yet checked)"
        )
        batch_results = _probe_batch(
            batch, target=target, timeout=timeout, workers=workers
        )
        # process fastest first within batch so we fill need with better ones
        batch_results.sort(
            key=lambda r: (0 if (r.ok and r.ms <= max_ms) else 1, r.ms if r.ok else 1e12)
        )

        for r in batch_results:
            all_checked.append(r)
            good = r.ok and r.ms <= max_ms
            if good and len(kept_rows) < need_n:
                kept_rows.append(r)
                _log(
                    f"[PROXY-CHECK]  KEEP  {r.label:<8}  {r.ms:7.0f} ms  "
                    f"HTTP {r.status}  [{len(kept_rows)}/{need_n}]  "
                    f"{mask_proxy(r.proxy)}"
                )
            elif good and len(kept_rows) >= need_n:
                # surplus good in same batch — still mark keep if we want extras?
                # User asked stop at need — treat surplus as unchecked preference:
                # keep only need; extras go back? Simpler: DROP from active pool as "surplus"
                # Actually better: if already have need, don't add more, count as not used
                dropped.append(r)
                _log(
                    f"[PROXY-CHECK]  SKIP  {r.label:<8}  {r.ms:7.0f} ms  "
                    f"surplus (already {need_n} good)  {mask_proxy(r.proxy)}"
                )
            else:
                dropped.append(r)
                if r.ok:
                    reason = f"slow >{max_ms:.0f}ms"
                else:
                    reason = (r.error or "fail")[:70]
                _log(
                    f"[PROXY-CHECK]  DROP  {r.label:<8}  {r.ms:7.0f} ms  "
                    f"{reason}  {mask_proxy(r.proxy)}"
                )

        if len(kept_rows) >= need_n:
            report.early_stop = True
            break

    report.elapsed_ms = (time.perf_counter() - t0) * 1000.0
    report.checked = all_checked
    report.dropped = dropped
    report.skipped_unchecked = len(queue)
    kept_rows.sort(key=lambda r: r.ms)
    report.kept = [r.proxy for r in kept_rows]

    early = (
        f"  early-stop, {report.skipped_unchecked} unchecked"
        if report.early_stop and report.skipped_unchecked
        else ""
    )
    _log(
        f"[PROXY-CHECK] done in {report.elapsed_ms/1000:.1f}s — "
        f"kept {report.kept_count}/{need_n} needed  "
        f"checked {report.total_checked}/{len(unique)}  "
        f"drop {report.drop_count}{early}"
    )
    if report.kept:
        best = kept_rows[0]
        worst = kept_rows[-1]
        mid = kept_rows[len(kept_rows) // 2]
        _log(
            f"[PROXY-CHECK] latency  best={best.ms:.0f}ms  "
            f"median≈{mid.ms:.0f}ms  worst_kept={worst.ms:.0f}ms"
        )
    return report


def apply_proxy_check(
    proxies: Sequence[str],
    *,
    enabled: bool = True,
    target: str = DEFAULT_CHECK_URL,
    max_ms: float = DEFAULT_MAX_MS,
    timeout: float = DEFAULT_TIMEOUT_SEC,
    workers: int = DEFAULT_WORKERS,
    need: Optional[int] = None,
    total_accounts: int = 0,
    concurrent: int = 1,
    proxy_mode: str = "per_account",
    require_one: bool = True,
    log: Optional[Callable[[str], None]] = None,
) -> List[str]:
    """
    Entry for pool/TUI.

    `need` wins if set; else derived from total_accounts / concurrent.
    """
    plist = [normalize_proxy(p) or str(p).strip() for p in proxies if p]
    plist = [p for p in plist if p]
    if not enabled or not plist:
        return plist

    need_n = resolve_proxy_need(
        total_accounts=total_accounts,
        concurrent=concurrent,
        proxy_count=len(plist),
        proxy_mode=proxy_mode,
        explicit_need=need,
    )

    report = filter_proxies(
        plist,
        target=target,
        max_ms=max_ms,
        timeout=timeout,
        workers=workers,
        need=need_n,
        log=log,
    )
    if not report.kept:
        msg = (
            f"no proxies passed health check to {target} "
            f"(need {need_n}, max {max_ms:.0f}ms). "
            f"Checked {report.total_checked}/{len(plist)}. "
            f"Raise --proxy-max-ms or fix proxy plan."
        )
        if require_one:
            raise RuntimeError(msg)
        if log:
            log(f"[PROXY-CHECK] WARN {msg} — using first {need_n} unfiltered")
        return plist[:need_n] if need_n else plist

    if report.kept_count < need_n:
        msg = (
            f"only {report.kept_count}/{need_n} proxies passed "
            f"(checked {report.total_checked}/{len(plist)}). "
            f"Continuing with {report.kept_count} good ones."
        )
        if log:
            log(f"[PROXY-CHECK] WARN {msg}")
        else:
            print(f"[PROXY-CHECK] WARN {msg}", flush=True)
        if report.kept_count == 0 and require_one:
            raise RuntimeError(msg)

    return report.kept

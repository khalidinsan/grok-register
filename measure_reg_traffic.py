#!/usr/bin/env python3
"""
Measure Camoufox network traffic + wall time for ONE full Grok registration.

  .venv/bin/python measure_reg_traffic.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

os.environ["GROK_BROWSER_ENGINE"] = "camoufox"
os.environ["GROK_DISPLAY"] = "offscreen"
os.environ["GROK_GEOIP"] = "false"
os.environ["GROK_HUMANIZE"] = "0"
os.environ.pop("GROK_BROWSER_PROXY", None)
os.environ.pop("BROWSER_PROXY", None)
os.environ.pop("GROK_PROXIES", None)

_stats = {
    "requests": 0,
    "responses": 0,
    "upload": 0,
    "download": 0,
    "download_unknown": 0,
    "errors": 0,
    "by_host": {},
}


def _host(url: str) -> str:
    try:
        from urllib.parse import urlsplit

        return urlsplit(url).hostname or "?"
    except Exception:
        return "?"


def _make_handlers():
    def on_request(req):
        try:
            _stats["requests"] += 1
            data = req.post_data
            if data:
                if isinstance(data, str):
                    _stats["upload"] += len(data.encode("utf-8", errors="replace"))
                else:
                    _stats["upload"] += len(data)
        except Exception:
            _stats["errors"] += 1

    def on_response(resp):
        try:
            _stats["responses"] += 1
            url = resp.url or ""
            host = _host(url)
            headers = resp.headers or {}
            cl = headers.get("content-length") or headers.get("Content-Length")
            size = int(cl) if cl and str(cl).isdigit() else 0
            if not size:
                _stats["download_unknown"] += 1
            _stats["download"] += size
            _stats["by_host"][host] = _stats["by_host"].get(host, 0) + size
        except Exception:
            _stats["errors"] += 1

    return on_request, on_response


def _attach_traffic(page, loop) -> None:
    """Register network listeners; async Playwright needs a running loop."""
    on_request, on_response = _make_handlers()

    async def _register():
        page.on("request", on_request)
        page.on("response", on_response)

    loop.run_until_complete(_register())


def _patch_camoufox():
    import browser_engine as be

    orig = be.launch_camoufox_session

    def wrapped(**kwargs):
        kwargs["proxy"] = ""
        kwargs["display"] = "offscreen"
        kwargs["humanize"] = 0.0
        sess = orig(**kwargs)
        raw = sess.extra.get("raw_page")
        loop = sess._loop
        if raw is not None and loop is not None:
            _attach_traffic(raw, loop)
            print("[measure] traffic hooks OK on Camoufox", flush=True)

        # Future pages (OAuth tab etc.)
        try:
            browser = sess.extra.get("browser")
            if browser and browser.contexts and loop is not None:
                on_request, on_response = _make_handlers()

                def on_page(page):
                    async def _reg():
                        page.on("request", on_request)
                        page.on("response", on_response)

                    try:
                        loop.run_until_complete(_reg())
                    except Exception as e:
                        print(f"[measure] new-page hook: {e}", flush=True)

                async def bind_all():
                    for ctx in browser.contexts:
                        ctx.on("page", on_page)
                        for p in ctx.pages:
                            if p is not raw:
                                p.on("request", on_request)
                                p.on("response", on_response)

                loop.run_until_complete(bind_all())
        except Exception as e:
            print(f"[measure] multi-page hook warn: {e}", flush=True)
        return sess

    be.launch_camoufox_session = wrapped

    import DrissionPage_example as farm

    def start_camoufox_only():
        from browser_engine import launch_camoufox_session

        farm.DISPLAY_MODE = "offscreen"
        print("[measure] forcing Camoufox (no chromium fallback)", flush=True)
        sess = launch_camoufox_session(proxy="", display="offscreen", humanize=0.0)
        farm._pw_context = sess  # type: ignore
        adapter = sess.extra.get("browser_adapter")
        farm.browser = adapter
        farm.page = adapter.latest_tab if adapter else sess.page
        farm._chrome_temp_dir = ""
        farm._chrome_debug_port = 0
        print("[measure] Camoufox ready", flush=True)

    farm.start_browser = start_camoufox_only


def _fmt_bytes(n: int) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.2f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0
    return f"{n:.2f} GB"


def main() -> int:
    _patch_camoufox()

    import DrissionPage_example as farm

    farm.DISPLAY_MODE = "offscreen"
    os.environ["GROK_DISPLAY"] = "offscreen"
    os.environ["GROK_BROWSER_ENGINE"] = "camoufox"
    if farm.run_logger is None:
        farm.run_logger = farm.setup_run_logger()
    try:
        farm._proxy_pool = []
        farm._browser_proxy = ""
    except Exception:
        pass

    out = ROOT / "sso" / f"measure_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    out.parent.mkdir(exist_ok=True)

    print("=" * 60, flush=True)
    print(" MEASURE: 1× register · Camoufox ONLY · NO PROXY", flush=True)
    print("=" * 60, flush=True)

    t0 = time.time()
    ok = False
    err = None
    email = ""
    phase_times: dict = {}

    try:
        farm.progress_begin_account(1)
        t1 = time.time()
        result = farm.run_single_registration(str(out), extract_numbers=False)
        phase_times["pipeline_s"] = time.time() - t1
        email = (result or {}).get("email") or ""
        farm.progress_end_account(True, f"email={email}")
        ok = True
    except Exception as e:
        err = str(e)
        phase_times["pipeline_s"] = time.time() - t0
        try:
            farm.progress_end_account(False, err[:180])
        except Exception:
            pass
        print(f"[measure] FAIL: {e}", flush=True)
    finally:
        try:
            farm.stop_browser()
        except Exception:
            pass

    elapsed = time.time() - t0
    body_total = _stats["upload"] + _stats["download"]
    # unknown responses often JS/chunked — rough 12KB avg when CL missing
    unknown_est = _stats["download_unknown"] * 12 * 1024
    wire_est = int((body_total + unknown_est) * 1.3)

    hosts = sorted(_stats["by_host"].items(), key=lambda x: -x[1])[:15]

    print(flush=True)
    print("=" * 60, flush=True)
    print(" RESULTS", flush=True)
    print("=" * 60, flush=True)
    print("  engine           : camoufox (forced)", flush=True)
    print("  proxy            : none", flush=True)
    print(f"  success          : {ok}", flush=True)
    if email:
        print(f"  email            : {email}", flush=True)
    if err:
        print(f"  error            : {err[:220]}", flush=True)
    print(f"  wall_time        : {elapsed:.1f}s  ({elapsed/60:.2f} min)", flush=True)
    print(f"  pipeline_time    : {phase_times.get('pipeline_s', elapsed):.1f}s", flush=True)
    print(flush=True)
    print("  --- traffic ---", flush=True)
    print(f"  requests         : {_stats['requests']}", flush=True)
    print(f"  responses        : {_stats['responses']}", flush=True)
    print(f"  upload (POST)    : {_fmt_bytes(_stats['upload'])}  ({_stats['upload']} B)", flush=True)
    print(f"  download (CL)    : {_fmt_bytes(_stats['download'])}  ({_stats['download']} B)", flush=True)
    print(
        f"  resp w/o CL      : {_stats['download_unknown']}  (est +{_fmt_bytes(unknown_est)})",
        flush=True,
    )
    print(f"  body total       : {_fmt_bytes(body_total)}", flush=True)
    print(
        f"  wire estimate    : {_fmt_bytes(wire_est)}  (body + unknown-est + ~30% overhead)",
        flush=True,
    )
    print(flush=True)
    print("  top hosts (download content-length):", flush=True)
    for h, b in hosts:
        print(f"    {_fmt_bytes(b):>10}  {h}", flush=True)
    print(flush=True)

    sticky_min = max(15, int(elapsed / 60) + 12)
    sticky_comfy = max(30, int(elapsed / 60) * 4 + 20)
    print("  --- sticky IP estimate (711 duration) ---", flush=True)
    print(f"  1 account wall   : ~{elapsed/60:.1f} min", flush=True)
    print(f"  sticky minimum   : {sticky_min} min", flush=True)
    print(
        f"  sticky comfort   : {min(180, sticky_comfy)}–60 min  (1–2 accounts / IP sequential)",
        flush=True,
    )
    print("  sticky roomy     : 90–180 min", flush=True)
    print(flush=True)

    per_mb = wire_est / (1024 * 1024) if wire_est else 0
    print("  --- bandwidth estimate (wire_est) ---", flush=True)
    print(f"  per register     : ~{per_mb:.2f} MB", flush=True)
    print(
        f"  × 50 accounts    : ~{per_mb * 50:.1f} MB  ({per_mb * 50 / 1024:.3f} GB)",
        flush=True,
    )
    print(
        f"  × 200 accounts   : ~{per_mb * 200:.1f} MB  ({per_mb * 200 / 1024:.3f} GB)",
        flush=True,
    )
    print(
        f"  × 1000 accounts  : ~{per_mb * 1000:.1f} MB  ({per_mb * 1000 / 1024:.2f} GB)",
        flush=True,
    )
    print("=" * 60, flush=True)

    report = ROOT / "logs" / f"traffic_measure_{time.strftime('%Y%m%d_%H%M%S')}.txt"
    report.parent.mkdir(exist_ok=True)
    report.write_text(
        f"ok={ok}\nemail={email}\nelapsed_s={elapsed:.1f}\n"
        f"upload_B={_stats['upload']}\ndownload_B={_stats['download']}\n"
        f"wire_est_B={wire_est}\nrequests={_stats['requests']}\n"
        f"unknown_resp={_stats['download_unknown']}\nhosts={hosts}\nerror={err}\n",
        encoding="utf-8",
    )
    print(f"  report: {report}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

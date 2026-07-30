"""Trusted handler for versioned OAuth probe+push queue jobs.

The queue CLI is intentionally configured with this module by farm_tui; job data
never selects imports or callables. Payload values may contain credentials and
must never be logged.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

JOB_KIND = "grok_probe_push"
JOB_VERSION = 1
_TOKEN_FIELDS = (
    "access_token", "refresh_token", "id_token", "expires_at", "expires_in",
    "email", "user_id", "team_id", "name", "scope", "referrer",
    "bot_flag_source", "jwt_claims", "auth_mode",
)


def make_payload(result: Mapping[str, Any], tokens: Any, *, job_id: str) -> dict[str, Any]:
    """Build the only accepted secret-bearing job schema."""
    token_data = {name: getattr(tokens, name) for name in _TOKEN_FIELDS if hasattr(tokens, name)}
    result_data = {
        key: result.get(key) for key in (
            "email", "password", "given_name", "family_name", "provider", "google",
            "_defer_account_ledger", "_sso_file", "_sso_line", "cookie_header",
            "build_email", "build_user_id", "_event_worker",
            "_event_account_index", "_event_attempt_id",
        ) if result.get(key) is not None
    }
    return {"version": 1, "operation": "probe", "data": {
        "kind": JOB_KIND, "schema_version": JOB_VERSION, "job_id": job_id,
        "tokens": token_data, "result": result_data,
    }}


def _emit_outcome(outcome: str, *, job_id: str, probe_status: int = 0,
                  email: str = "", worker: str = "", account_index: Any = None,
                  attempt_id: str = "") -> None:
    event = {"category": "probe", "event": "complete", "outcome": outcome,
             "job_id": job_id, "event_id": f"{job_id}:{outcome}",
             "probe_status": probe_status, "email": email,
             "worker": worker, "account_index": account_index,
             "attempt_id": attempt_id}
    event = {key: value for key, value in event.items() if value not in (None, "")}
    print("@@GROK_EVENT@@" + json.dumps(event, separators=(",", ":")), flush=True)


def _already_finalized(job_id: str) -> bool:
    path = Path(os.environ.get("GROK_ACCOUNTS_JSONL") or Path(__file__).parent / "accounts" / "accounts.jsonl")
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                if json.loads(line).get("probe_job_id") == job_id:
                    return True
            except (ValueError, TypeError):
                continue
    except OSError:
        pass
    return False


def tokens_from_payload(payload: Mapping[str, Any]) -> Any:
    """Validate and reconstruct the project's concrete BuildTokens value."""
    from build_oauth_pkce import BuildTokens
    data = payload.get("data")
    token_data = data.get("tokens") if isinstance(data, Mapping) else None
    if not isinstance(token_data, Mapping):
        raise ValueError("probe job has no token object")
    allowed = {k: v for k, v in token_data.items() if k in _TOKEN_FIELDS}
    if not allowed.get("access_token") or not allowed.get("refresh_token"):
        raise ValueError("probe job is missing OAuth tokens")
    return BuildTokens(**allowed)


def handle(payload: dict[str, Any]) -> None:
    """Reconstruct BuildTokens, invoke the existing pipeline, then finalize ledger."""
    if set(payload) != {"version", "operation", "data"} or payload.get("version") != 1 or payload.get("operation") != "probe":
        raise ValueError("unsupported queue envelope")
    data = payload.get("data")
    if not isinstance(data, dict) or data.get("kind") != JOB_KIND or data.get("schema_version") != JOB_VERSION:
        raise ValueError("unsupported probe job schema")
    job_id = str(data.get("job_id") or "")
    token_data, result = data.get("tokens"), data.get("result")
    if not job_id or not isinstance(token_data, dict) or not isinstance(result, dict):
        raise ValueError("incomplete probe job")
    if _already_finalized(job_id):
        if result.get("google") or result.get("_defer_account_ledger"):
            from google_signup import release_google_claim
            release_google_claim(str(result.get("email") or ""))
        return

    from DrissionPage_example import (
        ACCOUNT_STATUS_FAILED_PROBE, ACCOUNT_STATUS_FAILED_PUSH, ACCOUNT_STATUS_INJECTED,
        finalize_deferred_account_record, probe_and_push_grok_cli, _sync_account_status_from_result,
    )
    tokens = tokens_from_payload(payload)
    work = dict(result)
    work["_build_tokens"] = tokens
    email = str(work.get("email") or tokens.email or "")
    correlation = {
        "email": email,
        "worker": str(work.get("_event_worker") or ""),
        "account_index": work.get("_event_account_index"),
        "attempt_id": str(work.get("_event_attempt_id") or ""),
    }
    try:
        probe_and_push_grok_cli(work, tokens)
    except Exception as exc:
        # Record terminal failure only on the queue's final delivery attempt;
        # intermediate retries leave the prior oauth_ok/Google claim intact.
        attempt = int(os.environ.get("GROK_PROBE_JOB_ATTEMPT") or "0")
        maximum = int(os.environ.get("GROK_PROBE_JOB_MAX_ATTEMPTS") or "0")
        if maximum and attempt >= maximum:
            status = ACCOUNT_STATUS_FAILED_PROBE if "probe" in str(exc).lower() or "denied" in str(exc).lower() else ACCOUNT_STATUS_FAILED_PUSH
            if work.get("_defer_account_ledger") or work.get("google"):
                finalize_deferred_account_record(job_id=job_id, email=email,
                    password=str(work.get("password") or ""), mode="google", status=status,
                    extra={"provider": "google", "has_oauth": True, "error": str(exc)[:400]})
                try:
                    from google_signup import release_google_claim
                    release_google_claim(email)
                except Exception:
                    pass
            else:
                work["probe_job_id"] = job_id
                _sync_account_status_from_result(email, status=status, error=str(exc), result=work)
        if maximum and attempt >= maximum:
            _emit_outcome("hard_failed", job_id=job_id, **correlation)
        raise

    probe_status = int((work.get("chat_probe") or {}).get("status") or 0)
    inactive = work.get("inject_active") is False
    # External side effect has completed. Persist only non-secret delivery facts
    # before ledger/claim bookkeeping so stale finalization never repeats it.
    from probe_queue import ProbeQueue
    queue_path = os.environ.get("GROK_PROBE_QUEUE_PATH")
    if queue_path:
        queue = ProbeQueue(queue_path)
        from probe_queue import Job
        # Current lease identity is intentionally not exposed here; persist by
        # job id while it remains claimed by this process.
        with queue._connect() as conn:
            conn.execute("UPDATE jobs SET delivery_meta=?,updated_at=? WHERE id=? AND status='claimed'",
                         (json.dumps({"external_complete": True, "inactive": inactive,
                                      "probe_status": probe_status, "ledger_status":
                                      ACCOUNT_STATUS_FAILED_PROBE if inactive else ACCOUNT_STATUS_INJECTED},
                                     separators=(",", ":")), __import__("time").time(), job_id))
    # External 9router upsert is practically idempotent by provider identity; the
    # ledger marker prevents repeating it after a completed local finalization.
    ledger_status = ACCOUNT_STATUS_FAILED_PROBE if inactive else ACCOUNT_STATUS_INJECTED
    if work.get("_defer_account_ledger") or work.get("google"):
        finalize_deferred_account_record(
            job_id=job_id, email=email, password=str(work.get("password") or ""),
            given_name=str(work.get("given_name") or ""), family_name=str(work.get("family_name") or ""),
            sso_cookie=str(work.get("_sso_line") or work.get("cookie_header") or ""),
            mode="google", sso_file=str(work.get("_sso_file") or ""), status=ledger_status,
            extra={"provider": "google", "has_oauth": True,
                   "injected": not inactive, "imported": True,
                   "probe_status": probe_status, "inject_active": work.get("inject_active"),
                   "9router_id": work.get("9router_id") or ""},
        )
        from google_signup import release_google_claim
        release_google_claim(email)  # queue retry completes idempotent claim release
    else:
        work["probe_job_id"] = job_id
        _sync_account_status_from_result(email, status=ledger_status, result=work)
    _emit_outcome("inactive" if inactive else "usable", job_id=job_id,
                  probe_status=probe_status, **correlation)


def finalize_stale(payload: dict[str, Any], reason: str) -> None:
    """Bookkeeping-only dead letter; never repeat probe or external push."""
    data = payload.get("data") if isinstance(payload, dict) else {}
    job_id = str((data or {}).get("job_id") or "")
    result = (data or {}).get("result") if isinstance((data or {}).get("result"), dict) else {}
    from probe_queue import ProbeQueue
    meta = ProbeQueue(os.environ["GROK_PROBE_QUEUE_PATH"]).delivery_meta(job_id)
    from DrissionPage_example import ACCOUNT_STATUS_FAILED_PUSH, finalize_deferred_account_record
    status = str(meta.get("ledger_status") or ACCOUNT_STATUS_FAILED_PUSH)
    email = str(result.get("email") or "")
    if result.get("google") or result.get("_defer_account_ledger"):
        finalize_deferred_account_record(job_id=job_id, email=email,
            password=str(result.get("password") or ""), mode="google", status=status,
            extra={"provider": "google", "has_oauth": True,
                   "imported": bool(meta.get("external_complete")),
                   "injected": not bool(meta.get("inactive")),
                   "inject_active": False if meta.get("inactive") else None,
                   "probe_status": meta.get("probe_status"), "error": reason[:400]})
        from google_signup import release_google_claim
        release_google_claim(email)  # retry terminal bookkeeping if this fails
    _emit_outcome("inactive" if meta.get("inactive") else "hard_failed",
                  job_id=job_id, probe_status=int(meta.get("probe_status") or 0),
                  email=email, worker=str(result.get("_event_worker") or ""),
                  account_index=result.get("_event_account_index"),
                  attempt_id=str(result.get("_event_attempt_id") or ""))

"""Hybrid Grok registration: short browser harvest + protocol HTTP.

Modes (config ``register_mode`` / env ``GROK_REGISTER_MODE``):
  - browser  — full UI path (default)
  - hybrid   — harvest castle/cookies/next-action in browser, then
               CreateEmail / OTP / profile submit over curl_cffi
  - google   — Google OIDC from accounts/google_pass.txt (see google_signup.py)
"""
from __future__ import annotations

from .register import register_one_hybrid, resolve_register_mode

__all__ = ["register_one_hybrid", "resolve_register_mode"]

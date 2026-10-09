"""Per-request LLM usage telemetry for MailMind.

Diagnostics only: this module never changes model inputs/outputs or routing.
Each actual provider HTTP attempt is logged as one request in debug/llm_usage.txt.
"""
from __future__ import annotations

from contextlib import contextmanager
import contextvars
from datetime import datetime, timezone
from pathlib import Path
import threading

_BASE_DIR = Path(__file__).resolve().parent.parent
_USAGE_PATH = _BASE_DIR / "debug" / "llm_usage.txt"
_MAX_BYTES = 8 * 1024 * 1024
_LOCK = threading.Lock()
_CONTEXT = contextvars.ContextVar("mailmind_llm_usage_context", default=None)


def _clean(value, limit: int = 500) -> str:
    text = str(value or "").replace("\n", " ").replace("\r", " ").strip()
    return text[:limit]


def _email_context(email: dict | None) -> dict:
    value = email if isinstance(email, dict) else {}
    uid = str(value.get("uid") or value.get("message_id") or "").strip()
    thread_id = str(
        value.get("canonical_thread_id")
        or value.get("thread_id")
        or value.get("gmail_thread_id")
        or value.get("conversation_id")
        or uid
    ).strip()
    return {
        "scope": "email",
        "email_uid": uid,
        "thread_id": thread_id,
    }


def _batch_context(items) -> dict:
    emails = []
    for item in items or []:
        if isinstance(item, dict):
            email = item.get("email") if isinstance(item.get("email"), dict) else item
            if isinstance(email, dict):
                emails.append(email)
    uids = []
    for email in emails:
        uid = str(email.get("uid") or email.get("message_id") or "").strip()
        if uid and uid not in uids:
            uids.append(uid)
    return {
        "scope": "batch",
        "email_count": len(emails),
        "email_uids": ",".join(uids[:100]),
    }


@contextmanager
def usage_context(email: dict | None = None, *, batch_items=None):
    """Attach email/batch identity to all provider calls in the current context."""
    if batch_items is not None:
        context = _batch_context(batch_items)
    else:
        context = _email_context(email)
    token = _CONTEXT.set(context)
    try:
        yield context
    finally:
        _CONTEXT.reset(token)


def current_context() -> dict:
    value = _CONTEXT.get()
    return dict(value) if isinstance(value, dict) else {"scope": "unknown"}


def _trim_file_locked() -> None:
    try:
        if _USAGE_PATH.exists() and _USAGE_PATH.stat().st_size > _MAX_BYTES:
            backup = _USAGE_PATH.with_name(_USAGE_PATH.name + ".1")
            try:
                if backup.exists():
                    backup.unlink()
            except OSError:
                pass
            try:
                _USAGE_PATH.replace(backup)
            except OSError:
                pass
    except OSError:
        pass


def log_llm_usage(
    *,
    provider: str,
    model: str,
    operation: str,
    status: str,
    latency_seconds: float,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int | None = None,
    error_code: str = "",
    error_message: str = "",
    key_fingerprint: str = "",
    retry_attempt: int = 0,
) -> None:
    """Append one provider-attempt line to debug/llm_usage.txt and stdout."""
    context = current_context()
    if total_tokens is None:
        total_tokens = max(0, int(input_tokens)) + max(0, int(output_tokens))

    parts = [
        f"provider={_clean(provider, 40)}",
        f"model={_clean(model, 120)}",
        f"operation={_clean(operation, 120)}",
        "requests=1",
        f"input_tokens={max(0, int(input_tokens))}",
        f"output_tokens={max(0, int(output_tokens))}",
        f"total_tokens={max(0, int(total_tokens))}",
        f"status={_clean(status, 60)}",
        f"latency={max(0.0, float(latency_seconds)):.2f}s",
        f"scope={_clean(context.get('scope', 'unknown'), 20)}",
    ]

    for key in ("email_uid", "thread_id", "email_count", "email_uids"):
        value = context.get(key)
        if value not in (None, ""):
            parts.append(f"{key}={_clean(value, 1500)}")

    if error_code:
        parts.append(f"error_code={_clean(error_code, 60)}")
    if error_message:
        parts.append(f"error_message={_clean(error_message, 500)}")
    if key_fingerprint:
        parts.append(f"key_fingerprint={_clean(key_fingerprint, 32)}")
    if retry_attempt:
        parts.append(f"attempt={int(retry_attempt)}")

    line = f"[{datetime.now(timezone.utc).astimezone().isoformat(timespec='seconds')}] " + " ".join(parts)
    with _LOCK:
        try:
            _USAGE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _trim_file_locked()
            with _USAGE_PATH.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
    print(f"[LLM USAGE] {line}", flush=True)


def usage_log_path() -> str:
    return str(_USAGE_PATH)

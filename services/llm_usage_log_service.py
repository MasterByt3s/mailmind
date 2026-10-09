"""Privacy-safe LLM usage logging for MailMind.

Every provider request writes one compact record to terminal and
``debug/llm_usage.txt``. Email content, prompts, generated text, API keys,
and authorization headers are never written.

Usage can be attached to one email/thread through ``llm_usage_context``.
Batch operations keep their aggregate token counts accurate and identify the
emails included in the batch instead of pretending the aggregate is a per-email
exact count.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from functools import wraps
from pathlib import Path
import threading
from typing import Any, Callable, Iterator


_lock = threading.Lock()
_usage_context: ContextVar[dict[str, Any]] = ContextVar(
    "mailmind_llm_usage_context",
    default={},
)


def _log_path() -> Path:
    project_root = Path(__file__).resolve().parents[1]
    debug_dir = project_root / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    return debug_dir / "llm_usage.txt"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def current_llm_usage_context() -> dict[str, Any]:
    value = _usage_context.get()
    return dict(value) if isinstance(value, dict) else {}


@contextmanager
def llm_usage_context(
    *,
    email_uid: str = "",
    thread_id: str = "",
    email_uids: list[str] | tuple[str, ...] | None = None,
    scope: str = "email",
) -> Iterator[None]:
    """Attach stable email identifiers to all provider calls in this context."""
    normalized = [str(value or "").strip() for value in (email_uids or [])]
    normalized = [value for value in normalized if value]
    single_uid = str(email_uid or "").strip()
    if single_uid and single_uid not in normalized:
        normalized.insert(0, single_uid)

    payload = {
        "scope": str(scope or "email").strip().lower() or "email",
        "email_uid": single_uid,
        "thread_id": str(thread_id or "").strip(),
        "email_uids": normalized[:50],
    }
    token = _usage_context.set(payload)
    try:
        yield
    finally:
        _usage_context.reset(token)


def _context_fields() -> list[str]:
    ctx = current_llm_usage_context()
    scope = str(ctx.get("scope") or "").strip()
    email_uid = str(ctx.get("email_uid") or "").strip()
    thread_id = str(ctx.get("thread_id") or "").strip()
    email_uids = [str(value).strip() for value in (ctx.get("email_uids") or []) if str(value).strip()]

    fields: list[str] = []
    if scope:
        fields.append(f"scope={scope}")
    if email_uid:
        fields.append(f"email_uid={email_uid}")
    elif len(email_uids) == 1:
        fields.append(f"email_uid={email_uids[0]}")
    if thread_id:
        fields.append(f"thread_id={thread_id}")
    if len(email_uids) > 1:
        fields.append(f"email_count={len(email_uids)}")
        fields.append("email_uids=" + ",".join(email_uids))
    return fields


def _trace_fields() -> list[str]:
    # Keep generation trace metadata optional. The usage logger must stay usable
    # for background tasks that are not summary/draft workers.
    try:
        from services.generation_trace_service import current_trace_context

        ctx = current_trace_context()
    except Exception:
        ctx = {}
    if not isinstance(ctx, dict):
        return []

    fields: list[str] = []
    kind = str(ctx.get("kind") or "").strip()
    job_id = str(ctx.get("job_id") or "").strip()
    call_index = ctx.get("ollama_calls")
    if kind:
        fields.append(f"trace={kind}")
    if job_id:
        fields.append(f"job={job_id}")
    if call_index:
        fields.append(f"trace_calls={call_index}")
    return fields


def log_llm_usage(
    *,
    provider: str,
    model: str,
    operation: str,
    request_count: int = 1,
    input_tokens: int = 0,
    output_tokens: int = 0,
    total_tokens: int | None = None,
    status: str = "success",
    latency_seconds: float | None = None,
    retries: int = 0,
    rate_limit_remaining_requests: int | str | None = None,
    rate_limit_remaining_tokens: int | str | None = None,
    rate_limit_reset_requests: str | None = None,
    rate_limit_reset_tokens: str | None = None,
) -> None:
    """Append one privacy-safe usage record to terminal and ``debug/llm_usage.txt``."""
    provider = str(provider or "unknown").strip().lower()
    model = str(model or "unknown").strip()
    operation = str(operation or "LLM").strip()
    status = str(status or "success").strip().lower()
    request_count = max(0, int(request_count or 0))
    input_tokens = max(0, int(input_tokens or 0))
    output_tokens = max(0, int(output_tokens or 0))
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    total_tokens = max(0, int(total_tokens or 0))
    retries = max(0, int(retries or 0))

    latency: float | None = None
    if latency_seconds is not None:
        try:
            latency = max(0.0, float(latency_seconds))
        except (TypeError, ValueError):
            latency = None

    fields = [
        f"[{_now()}]",
        f"provider={provider}",
        f"model={model}",
        f"operation={operation}",
        f"requests={request_count}",
        f"input_tokens={input_tokens}",
        f"output_tokens={output_tokens}",
        f"total_tokens={total_tokens}",
        f"status={status}",
    ]
    fields.extend(_context_fields())
    if retries:
        fields.append(f"retries={retries}")
    if latency is not None:
        fields.append(f"latency={latency:.2f}s")
    if rate_limit_remaining_requests is not None:
        fields.append(f"rate_remaining_requests={rate_limit_remaining_requests}")
    if rate_limit_remaining_tokens is not None:
        fields.append(f"rate_remaining_tokens={rate_limit_remaining_tokens}")
    if rate_limit_reset_requests:
        fields.append(f"rate_reset_requests={rate_limit_reset_requests}")
    if rate_limit_reset_tokens:
        fields.append(f"rate_reset_tokens={rate_limit_reset_tokens}")
    fields.extend(_trace_fields())

    line = " ".join(str(part) for part in fields if str(part).strip())
    print(f"[LLM Usage] {line}", flush=True)
    try:
        with _lock:
            path = _log_path()
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except Exception:
        # Telemetry must never change application behavior.
        pass


def _single_email_from_args(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any] | None:
    for value in args:
        if isinstance(value, dict) and str(value.get("uid") or "").strip():
            return value
    for value in kwargs.values():
        if isinstance(value, dict) and str(value.get("uid") or "").strip():
            return value
    return None


def track_email_usage(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator for one-email AI functions without changing their signatures."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        email = _single_email_from_args(args, kwargs)
        if not email:
            return func(*args, **kwargs)
        with llm_usage_context(
            email_uid=str(email.get("uid") or ""),
            thread_id=str(email.get("canonical_thread_id") or ""),
            scope="email",
        ):
            return func(*args, **kwargs)

    return wrapper


def track_batch_email_usage(func: Callable[..., Any]) -> Callable[..., Any]:
    """Decorator for AI functions that submit several emails in one request."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        items = args[0] if args else kwargs.get("items")
        uids: list[str] = []
        if isinstance(items, (list, tuple)):
            for item in items:
                if not isinstance(item, dict):
                    continue
                email = item.get("email") if isinstance(item.get("email"), dict) else item
                uid = str(email.get("uid") or "").strip() if isinstance(email, dict) else ""
                if uid and uid not in uids:
                    uids.append(uid)
        with llm_usage_context(email_uids=uids, scope="batch"):
            return func(*args, **kwargs)

    return wrapper

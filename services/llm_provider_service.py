"""Central LLM routing for MailMind: Groq -> Cloudflare -> Ollama.

OpenRouter is intentionally removed from the runtime path.
"""

from __future__ import annotations

import json
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import (
    CLOUDFLARE_ACCOUNT_ID,
    CLOUDFLARE_API_TOKEN,
    CLOUDFLARE_MODEL,
    CLOUDFLARE_REASONING_EFFORT,
    CLOUDFLARE_REJECT_IF_BUSY,
    CLOUDFLARE_REQUEST_TIMEOUT,
    CLOUDFLARE_TEMPERATURE,
    GROQ_API_KEY,
    GROQ_MODEL,
    GROQ_REASONING_EFFORT,
    GROQ_REQUEST_TIMEOUT,
    GROQ_TEMPERATURE,
    LLM_FALLBACK_ENABLED,
    LLM_FALLBACK_PROVIDERS,
    LLM_PROVIDER,
    OLLAMA_KEEP_ALIVE,
    OLLAMA_MODEL,
    OLLAMA_REQUEST_TIMEOUT,
    OLLAMA_TEMPERATURE,
    OLLAMA_THINK,
    OLLAMA_URL,
)
from services.generation_trace_service import consume_next_ollama_operation
from services.llm_usage_service import current_context, log_llm_usage, usage_context
from services.ollama_runtime_service import log_ollama_timing, ollama_generation_slot

GROQ_MAX_COMPLETION_TOKENS = 900

_lock = threading.Lock()
_last_provider = None
_last_model = None
_last_operation = None
_last_used_at = 0.0

def _log_groq_configuration_once() -> None:
    state = getattr(_log_groq_configuration_once, "_done", False)
    if state:
        return
    _log_groq_configuration_once._done = True
    print(
        f"[GROQ] configured={bool(GROQ_API_KEY)} model={GROQ_MODEL} "
        f"key_fingerprint={_safe_key_fingerprint(GROQ_API_KEY)}",
        flush=True,
    )

def _set_current(provider: str, model: str, operation: str, *, fallback: bool = False) -> None:
    global _last_provider, _last_model, _last_operation, _last_used_at
    with _lock:
        _last_provider = provider
        _last_model = model
        _last_operation = operation
        _last_used_at = time.time()
    suffix = " [FALLBACK]" if fallback else ""
    print(f"[LLM] {provider.upper()}{suffix} | model={model} | operation={operation}", flush=True)

def current_llm_status() -> dict:
    with _lock:
        provider, model, operation = _last_provider, _last_model, _last_operation
    configured = LLM_PROVIDER
    fallback_model = {
        "groq": GROQ_MODEL,
        "cloudflare": CLOUDFLARE_MODEL,
        "ollama": OLLAMA_MODEL,
        "auto": GROQ_MODEL,
    }.get(configured, GROQ_MODEL)
    return {
        "provider": provider or configured,
        "model": model or fallback_model,
        "operation": operation or "idle",
        "configured": configured,
        "fallback_enabled": bool(LLM_FALLBACK_ENABLED),
        "fallback_providers": list(LLM_FALLBACK_PROVIDERS),
    }

def _provider_chain() -> list[str]:
    primary = LLM_PROVIDER
    if primary == "ollama":
        return ["ollama"]
    if primary == "auto":
        return ["groq", "cloudflare", "ollama"]
    chain = [primary]
    if LLM_FALLBACK_ENABLED:
        for value in LLM_FALLBACK_PROVIDERS:
            if value in {"groq", "cloudflare", "ollama"} and value not in chain:
                chain.append(value)
    return chain

def _extract_openai_text(envelope: dict) -> str:
    try:
        content = envelope["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as error:
        raise RuntimeError("Provider returned an unexpected chat response.") from error
    if content is None:
        raise RuntimeError("Provider returned an empty response.")
    return str(content).strip()

def _token_usage(envelope: dict) -> tuple[int, int, int, bool]:
    usage = envelope.get("usage") if isinstance(envelope, dict) else None
    if not isinstance(usage, dict):
        return 0, 0, 0, False
    try:
        prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    except (TypeError, ValueError):
        prompt = 0
    try:
        output = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    except (TypeError, ValueError):
        output = 0
    try:
        total = int(usage.get("total_tokens") or (prompt + output))
    except (TypeError, ValueError):
        total = prompt + output
    return prompt, output, total, True

def _safe_key_fingerprint(value: str) -> str:
    if not value:
        return "missing"
    digest = __import__("hashlib").sha256(value.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:12]}"

def _safe_error_detail(detail: str) -> str:
    text = str(detail or "").replace("\n", " ").replace("\r", " ").strip()
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1[REDACTED]", text)
    text = re.sub(r"(?i)(gsk_[A-Za-z0-9_-]{8,})", "[REDACTED_GROQ_KEY]", text)
    return text[:500]

def _raise_http(provider: str, error: HTTPError) -> None:
    detail = error.read().decode("utf-8", errors="replace")
    raise RuntimeError(f"{provider.title()} request failed (HTTP {error.code}): {detail}") from error

def _post_groq(payload: dict, *, operation: str, attempt: int) -> dict:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not configured.")
    started = time.perf_counter()
    request = Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {GROQ_API_KEY}",
            "User-Agent": "MailMind/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=GROQ_REQUEST_TIMEOUT) as response:
            envelope = json.loads(response.read().decode("utf-8"))
        latency = time.perf_counter() - started
        inp, out, total, _ = _token_usage(envelope)
        log_llm_usage(
            provider="groq", model=GROQ_MODEL, operation=operation,
            status="success", latency_seconds=latency,
            input_tokens=inp, output_tokens=out, total_tokens=total,
            retry_attempt=attempt,
        )
        return envelope
    except HTTPError as error:
        latency = time.perf_counter() - started
        detail = error.read().decode("utf-8", errors="replace")
        safe_detail = _safe_error_detail(detail)
        key_fp = _safe_key_fingerprint(GROQ_API_KEY)
        log_llm_usage(
            provider="groq", model=GROQ_MODEL, operation=operation,
            status=f"http_{error.code}", latency_seconds=latency,
            error_code=str(error.code), error_message=safe_detail,
            key_fingerprint=key_fp, retry_attempt=attempt,
        )
        raise RuntimeError(f"Groq request failed (HTTP {error.code}): {safe_detail}") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        latency = time.perf_counter() - started
        status = "timeout" if isinstance(error, TimeoutError) else "failed"
        safe_detail = _safe_error_detail(str(error))
        log_llm_usage(
            provider="groq", model=GROQ_MODEL, operation=operation,
            status=status, latency_seconds=latency,
            error_message=safe_detail,
            key_fingerprint=_safe_key_fingerprint(GROQ_API_KEY),
            retry_attempt=attempt,
        )
        raise RuntimeError(f"Could not complete Groq request: {safe_detail}") from error

def _post_cloudflare(payload: dict, *, operation: str, attempt: int) -> dict:
    if not CLOUDFLARE_ACCOUNT_ID:
        raise RuntimeError("CLOUDFLARE_ACCOUNT_ID is not configured.")
    if not CLOUDFLARE_API_TOKEN:
        raise RuntimeError("CLOUDFLARE_API_TOKEN is not configured.")
    started = time.perf_counter()
    url = (
        "https://api.cloudflare.com/client/v4/accounts/"
        f"{CLOUDFLARE_ACCOUNT_ID}/ai/v1/chat/completions"
    )
    request = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {CLOUDFLARE_API_TOKEN}",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=CLOUDFLARE_REQUEST_TIMEOUT) as response:
            envelope = json.loads(response.read().decode("utf-8"))
        latency = time.perf_counter() - started
        inp, out, total, _ = _token_usage(envelope)
        log_llm_usage(
            provider="cloudflare", model=CLOUDFLARE_MODEL, operation=operation,
            status="success", latency_seconds=latency,
            input_tokens=inp, output_tokens=out, total_tokens=total,
            retry_attempt=attempt,
        )
        return envelope
    except HTTPError as error:
        latency = time.perf_counter() - started
        detail = error.read().decode("utf-8", errors="replace")
        log_llm_usage(
            provider="cloudflare", model=CLOUDFLARE_MODEL, operation=operation,
            status=f"http_{error.code}", latency_seconds=latency,
            error_code=str(error.code), retry_attempt=attempt,
        )
        raise RuntimeError(f"Cloudflare request failed (HTTP {error.code}): {detail}") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        latency = time.perf_counter() - started
        status = "timeout" if isinstance(error, TimeoutError) else "failed"
        log_llm_usage(
            provider="cloudflare", model=CLOUDFLARE_MODEL, operation=operation,
            status=status, latency_seconds=latency, retry_attempt=attempt,
        )
        raise RuntimeError(f"Could not complete Cloudflare request: {error}") from error

def _post_ollama(payload: dict, operation: str, attempt: int) -> dict:
    started = time.perf_counter()
    request = Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        slot_wait_started = time.perf_counter()
        with ollama_generation_slot():
            slot_wait = time.perf_counter() - slot_wait_started
            http_started = time.perf_counter()
            with urlopen(request, timeout=OLLAMA_REQUEST_TIMEOUT) as response:
                envelope = json.loads(response.read().decode("utf-8"))
            http_elapsed = time.perf_counter() - http_started
        latency = time.perf_counter() - started
        print(
            "[Ollama transport] "
            f"operation={operation} slot_wait={slot_wait:.3f}s "
            f"http={http_elapsed:.3f}s total={latency:.3f}s",
            flush=True,
        )
        try:
            log_ollama_timing(envelope, consume_next_ollama_operation(operation))
        except Exception:
            pass
        inp = int(envelope.get("prompt_eval_count") or 0)
        out = int(envelope.get("eval_count") or 0)
        log_llm_usage(
            provider="ollama", model=OLLAMA_MODEL, operation=operation,
            status="success", latency_seconds=latency,
            input_tokens=inp, output_tokens=out, total_tokens=inp + out,
            retry_attempt=attempt,
        )
        return envelope
    except HTTPError as error:
        latency = time.perf_counter() - started
        slot_wait = locals().get("slot_wait", 0.0)
        http_elapsed = locals().get("http_elapsed", max(0.0, latency - float(slot_wait or 0.0)))
        print(
            "[Ollama transport] "
            f"operation={operation} slot_wait={float(slot_wait or 0.0):.3f}s "
            f"http={float(http_elapsed or 0.0):.3f}s total={latency:.3f}s "
            f"status=http_{error.code}",
            flush=True,
        )
        detail = error.read().decode("utf-8", errors="replace")
        log_llm_usage(
            provider="ollama", model=OLLAMA_MODEL, operation=operation,
            status=f"http_{error.code}", latency_seconds=latency,
            error_code=str(error.code), retry_attempt=attempt,
        )
        raise RuntimeError(f"Ollama request failed (HTTP {error.code}): {detail}") from error
    except (URLError, TimeoutError, json.JSONDecodeError) as error:
        latency = time.perf_counter() - started
        slot_wait = locals().get("slot_wait", 0.0)
        http_elapsed = locals().get("http_elapsed", max(0.0, latency - float(slot_wait or 0.0)))
        status = "timeout" if isinstance(error, TimeoutError) else "failed"
        print(
            "[Ollama transport] "
            f"operation={operation} slot_wait={float(slot_wait or 0.0):.3f}s "
            f"http={float(http_elapsed or 0.0):.3f}s total={latency:.3f}s "
            f"status={status}",
            flush=True,
        )
        log_llm_usage(
            provider="ollama", model=OLLAMA_MODEL, operation=operation,
            status=status, latency_seconds=latency, retry_attempt=attempt,
        )
        raise RuntimeError(f"Could not complete Ollama request: {error}") from error

def _payload(messages, *, model: str, temperature: float, response_format=None, reasoning_effort=None, cloudflare=False) -> dict:
    value = {
        "model": model,
        "messages": messages,
        "stream": False,
        "temperature": temperature,
    }
    if response_format is not None:
        value["response_format"] = response_format
    if reasoning_effort:
        value["reasoning_effort"] = reasoning_effort
    return value

def _request_with_provider(provider: str, messages: list[dict], *, response_format=None, temperature=None, operation="LLM", retry=False, attempt: int = 1) -> str:
    if provider == "groq":
        payload = _payload(
            messages,
            model=GROQ_MODEL,
            temperature=GROQ_TEMPERATURE if temperature is None else temperature,
            response_format=response_format,
            reasoning_effort=GROQ_REASONING_EFFORT,
        )
        payload["max_completion_tokens"] = GROQ_MAX_COMPLETION_TOKENS
        return _extract_openai_text(_post_groq(payload, operation=operation, attempt=attempt))

    if provider == "cloudflare":
        payload = _payload(
            messages,
            model=CLOUDFLARE_MODEL,
            temperature=CLOUDFLARE_TEMPERATURE if temperature is None else temperature,
            response_format=response_format,
            reasoning_effort=CLOUDFLARE_REASONING_EFFORT,
            cloudflare=True,
        )
        try:
            return _extract_openai_text(_post_cloudflare(payload, operation=operation, attempt=attempt))
        except RuntimeError:
            if CLOUDFLARE_REJECT_IF_BUSY:
                raise
            raise

    if provider == "ollama":
        payload = _payload(
            messages,
            model=OLLAMA_MODEL,
            temperature=OLLAMA_TEMPERATURE if temperature is None else temperature,
        )
        payload.update({
            "think": OLLAMA_THINK,
            "keep_alive": OLLAMA_KEEP_ALIVE,
        })
        if response_format is not None:
            if (
                isinstance(response_format, dict)
                and response_format.get("type") == "json_schema"
                and isinstance(response_format.get("json_schema"), dict)
            ):
                payload["format"] = response_format["json_schema"].get("schema")
            else:
                payload["format"] = response_format
        return json.loads(json.dumps(_post_ollama(payload, operation, attempt))).get("message", {}).get("content", "").strip()

    raise RuntimeError(f"Unsupported LLM provider: {provider}")

def _run(messages: list[dict], *, response_format=None, temperature=None, operation="LLM", retry=False) -> str:
    _log_groq_configuration_once()
    providers = _provider_chain()
    last_error = None
    for index, provider in enumerate(providers):
        attempt = index + 1
        try:
            content = _request_with_provider(
                provider, messages,
                response_format=response_format,
                temperature=temperature,
                operation=operation,
                retry=retry,
                attempt=attempt,
            )
            model = {
                "groq": GROQ_MODEL,
                "cloudflare": CLOUDFLARE_MODEL,
                "ollama": OLLAMA_MODEL,
            }[provider]
            _set_current(provider, model, operation, fallback=index > 0)
            return content
        except Exception as error:
            last_error = error
            print(f"[LLM] {provider.upper()} FAILED | operation={operation} | {error}", flush=True)
            if index + 1 < len(providers):
                print(f"[LLM] Switching to {providers[index + 1].upper()} fallback...", flush=True)
    raise RuntimeError(f"All configured LLM providers failed for {operation}: {last_error}") from last_error

def request_text(system_prompt: str, user_input: str, *, operation: str = "text generation") -> str:
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_input},
    ]
    return _run(messages, operation=operation)

def request_llm_structured(
    prompt: str,
    schema: dict,
    *,
    operation: str = "structured output",
    retry: bool = False,
) -> dict:
    schema_instruction = (
        "\n\nSTRUCTURED OUTPUT CONTRACT:\n"
        "Return one JSON object that matches this JSON Schema exactly. "
        "Do not add markdown, commentary, or fields outside the schema.\n"
        f"{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}"
    )
    if retry:
        schema_instruction += (
            "\nThe previous response failed structural validation. "
            "Regenerate the answer from the source and satisfy the schema exactly."
        )
    messages = [{"role": "user", "content": prompt + schema_instruction}]
    response_format = {
        "type": "json_schema",
        "json_schema": {
            "name": "mailmind_structured_response",
            "strict": True,
            "schema": schema,
        },
    }
    content = _run(messages, response_format=response_format, operation=operation, retry=retry)
    try:
        value = json.loads(content)
    except (TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError("Provider returned invalid JSON for structured output.") from error
    if not isinstance(value, dict):
        raise ValueError("Structured provider response is not an object.")
    return value

request_structured = request_llm_structured

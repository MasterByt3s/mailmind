# Dedicated To-Do task-title generator.
#
# This service is intentionally separate from email summarization.  It only turns
# an already-extracted action item into a short task title for the To-Do UI.
from __future__ import annotations

import json
import re
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import (
    OLLAMA_KEEP_ALIVE,
    OLLAMA_MODEL,
    OLLAMA_REQUEST_TIMEOUT,
    OLLAMA_TEMPERATURE,
    OLLAMA_THINK,
    OLLAMA_URL,
    TODO_TITLE_BATCH_SIZE,
)
from services.ollama_runtime_service import log_ollama_timing
from services.llm_provider_service import request_structured as request_llm_structured




def _clean_source_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip())


def _fallback_task_title(action_item: str) -> str:
    # Create a stable local fallback if Ollama is unavailable or returns bad data.
    text = _clean_source_text(action_item)
    if not text:
        return "Untitled task"

    # Remove common deadline tails so the title stays focused on the action.
    text = re.sub(
        r"\s+(?:by|before|on|until|no later than)\s+"
        r"(?:today|tomorrow|(?:mon|tues|wednes|thurs|fri|satur|sun)day|"
        r"[A-Z][a-z]{2,8}\s+\d{1,2}(?:,\s*\d{4})?|\d{4}-\d{2}-\d{2}|"
        r"\d{1,2}/\d{1,2}/\d{2,4})(?:\s+at\s+[^,.;]+)?[. ]*$",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = text.strip(" \t\r\n-–—:;,.\"'")

    # Keep the fallback compact; the LLM path normally does the semantic rewrite.
    words = text.split()
    if len(words) > 8:
        text = " ".join(words[:8]).rstrip(" ,;:-")
    return text or "Untitled task"


def _sanitize_title(value: str, fallback_source: str) -> str:
    title = _clean_source_text(value)
    title = re.sub(r"^(?:task\s*title|title)\s*:\s*", "", title, flags=re.IGNORECASE)
    title = title.strip(" \t\r\n-–—:;,.\"'")
    if not title:
        return _fallback_task_title(fallback_source)

    # Guard against chatty model output even though the prompt asks for title only.
    if "\n" in title:
        title = title.splitlines()[0].strip()
    words = title.split()
    if len(words) > 10:
        title = " ".join(words[:10]).rstrip(" ,;:-")
    return title or _fallback_task_title(fallback_source)


def _request_titles(action_items: list[str]) -> list[str]:
    prompt = f"""You generate task titles for a To-Do List. You are NOT summarizing emails.
The action items below were already extracted by another system.

Return JSON only with exactly one key: titles. The value of titles must be an array with exactly {len(action_items)} strings, in the same order as the input.

Rules: 3 to 8 words; clear action verb when possible; no due dates, priority, status, labels, explanations, numbering, or invented information.

Action items:
{json.dumps(action_items, ensure_ascii=False, indent=2)}
"""
    parsed = request_llm_structured(prompt, {
        "type": "object", "properties": {"titles": {"type": "array", "items": {"type": "string"}}},
        "required": ["titles"], "additionalProperties": False,
    }, operation="todo title")
    titles = parsed.get("titles")
    if not isinstance(titles, list) or len(titles) != len(action_items):
        raise ValueError("Task title response length did not match the input length.")
    return [str(value or "").strip() for value in titles]


def generate_task_titles(action_items: list[str]) -> list[str]:
    # Generate concise To-Do titles in one LLM call, with a safe local fallback.
    #
    # The function is designed for the visible To-Do page (up to 10 rows), so title
    # generation does not add a second LLM call to the summary-generation workflow.
    sources = [_clean_source_text(item) for item in action_items]
    if not sources:
        return []

    final: list[str] = []
    for start in range(0, len(sources), TODO_TITLE_BATCH_SIZE):
        chunk = sources[start:start + TODO_TITLE_BATCH_SIZE]
        try:
            generated = _request_titles(chunk)
        except (HTTPError, URLError, TimeoutError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            generated = [""] * len(chunk)
        final.extend(
            _sanitize_title(title, source)
            for title, source in zip(generated, chunk)
        )
    return final

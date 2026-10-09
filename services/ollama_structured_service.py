# Central Ollama adapter for schema-constrained JSON responses.
import json
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from config import (
    OLLAMA_KEEP_ALIVE,
    OLLAMA_MODEL,
    OLLAMA_REQUEST_TIMEOUT,
    OLLAMA_RETRY_TEMPERATURE,
    OLLAMA_TEMPERATURE,
    OLLAMA_THINK,
    OLLAMA_URL,
)
from services.ollama_runtime_service import log_ollama_timing, ollama_generation_slot
from services.summary_profiler_service import record_summary_ollama_call
from services.llm_provider_service import request_llm_structured


class StructuredOutputError(RuntimeError):
    # Raised only when transport succeeds but the model cannot satisfy the JSON contract.
    pass


def _matches_type(value, expected) -> bool:
    if isinstance(expected, list):
        return any(_matches_type(value, item) for item in expected)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "null":
        return value is None
    return True


def _validate_schema(value, schema: dict, path: str = "$" ) -> None:
    expected = schema.get("type")
    if expected is not None and not _matches_type(value, expected):
        raise ValueError(f"{path} has the wrong type")

    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{path} is outside the allowed values")

    if isinstance(value, dict):
        required = schema.get("required") or []
        missing = [key for key in required if key not in value]
        if missing:
            raise ValueError(f"{path} is missing required field(s): {', '.join(missing)}")

        properties = schema.get("properties") or {}
        if schema.get("additionalProperties") is False:
            extras = [key for key in value if key not in properties]
            if extras:
                raise ValueError(f"{path} contains unexpected field(s): {', '.join(extras)}")

        for key, child_schema in properties.items():
            if key in value:
                _validate_schema(value[key], child_schema, f"{path}.{key}")

    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for index, item in enumerate(value):
            _validate_schema(item, schema["items"], f"{path}[{index}]")


def _schema_prompt(prompt: str, schema: dict, retry: bool) -> str:
    suffix = (
        "\n\nSTRUCTURED OUTPUT CONTRACT:\n"
        "Return one JSON object that matches this JSON Schema exactly. "
        "Do not add markdown, commentary, or fields outside the schema.\n"
        f"{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}"
    )
    if retry:
        suffix += (
            "\nThe previous response failed structural validation. "
            "Regenerate the answer from the source and satisfy the schema exactly."
        )
    return prompt + suffix


def request_structured(prompt: str, schema: dict, *, operation: str = "structured output") -> dict:
    # Provider selection/fallback is centralized; keep local schema validation here.
    last_error = None
    for attempt in range(2):
        try:
            result = request_llm_structured(prompt, schema, operation=operation, retry=bool(attempt))
            _validate_schema(result, schema)
            return result
        except Exception as error:
            last_error = error
            if attempt == 0:
                continue
    raise StructuredOutputError(
        f"No valid {operation} after provider request and one schema-constrained retry."
    ) from last_error


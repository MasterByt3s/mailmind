"""Compatibility wrapper. Runtime LLM routing lives in llm_provider_service."""
from services.llm_provider_service import (
    current_llm_status,
    request_chat,
    request_llm_structured,
    request_structured,
    request_text,
)

__all__ = [
    "current_llm_status",
    "request_chat",
    "request_llm_structured",
    "request_structured",
    "request_text",
]

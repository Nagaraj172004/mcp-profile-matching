"""Groq chat model shared by the agent and its tools.

Every caller treats the model as optional: if the key is missing, USE_LLM=0
is set, or a call fails, the caller falls back to a rule-based answer so the
screening pipeline still runs.
"""

from __future__ import annotations

import os
import sys
import time
from typing import Callable, Optional

from dotenv import load_dotenv

from tls import use_system_certificates

load_dotenv()

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
MODEL = os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b")
# Groq counts the requested max_tokens against the per-minute output limit, and
# the default is the model maximum, so back-to-back calls get rejected without a cap.
MAX_TOKENS = int(os.environ.get("LLM_MAX_TOKENS", "4096"))

_llm = None


def llm_enabled() -> bool:
    if os.environ.get("USE_LLM", "1").strip() == "0":
        return False
    return bool(os.environ.get("GROQ_API_KEY", "").strip())


def get_llm():
    """Return a ChatOpenAI client pointed at Groq, or None when disabled."""
    global _llm
    if not llm_enabled():
        return None
    if _llm is None:
        use_system_certificates()
        from langchain_openai import ChatOpenAI

        _llm = ChatOpenAI(
            model=MODEL,
            api_key=os.environ["GROQ_API_KEY"].strip(),
            base_url=GROQ_BASE_URL,
            temperature=0,
            max_tokens=MAX_TOKENS,
            max_retries=2,
            timeout=60,
        )
    return _llm


PER_MINUTE_WAIT_SECONDS = 20
PER_MINUTE_RETRIES = 3


def _warn(where: str, exc: Exception) -> None:
    print("[llm] {0} failed, using fallback: {1}".format(where, str(exc)[:200]), file=sys.stderr)


def is_per_minute_limit(exc: Exception) -> bool:
    """Per-minute quotas (RPM, TPM, OTPM) clear within a minute; the daily one (TPD) does not."""
    message = str(exc)
    return "429" in message and "per minute" in message


def call(fn: Callable, where: str = "model call"):
    """Run fn, waiting out per-minute rate limits instead of failing."""
    for attempt in range(PER_MINUTE_RETRIES + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == PER_MINUTE_RETRIES or not is_per_minute_limit(exc):
                raise
            print("[llm] {0}: per-minute limit reached, waiting {1}s".format(where, PER_MINUTE_WAIT_SECONDS), file=sys.stderr)
            time.sleep(PER_MINUTE_WAIT_SECONDS)


def structured(schema, messages, where: str = "structured call"):
    """Invoke the model and parse its answer into a pydantic schema."""
    llm = get_llm()
    if llm is None:
        return None
    for attempt in range(2):
        try:
            return call(lambda: llm.with_structured_output(schema, method="function_calling").invoke(messages), where)
        except Exception as exc:
            if attempt == 1 or "429" in str(exc):
                _warn(where, exc)
                return None
    return None


def text(messages, where: str = "text call") -> Optional[str]:
    llm = get_llm()
    if llm is None:
        return None
    try:
        return (call(lambda: llm.invoke(messages), where).content or "").strip() or None
    except Exception as exc:
        _warn(where, exc)
        return None

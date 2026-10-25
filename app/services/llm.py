"""OpenRouter access via the OpenAI-compatible API.

All model calls go through `get_llm()`, so tests can swap in a fake with
`set_llm()`. Model names always come from settings, never from code.
"""

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from app.config import BASE_DIR, get_settings

log = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)
PROMPTS_DIR = BASE_DIR / "prompts"


class LLMError(RuntimeError):
    pass


class LLM(Protocol):
    def complete(self, model: str, messages: list[dict], json_mode: bool = False, temperature: float = 0.4) -> str: ...

    def stream(self, model: str, messages: list[dict], temperature: float = 0.5) -> AsyncIterator[str]: ...


class OpenRouterLLM:
    def __init__(self):
        s = get_settings()
        if not s.openrouter_api_key:
            raise LLMError("OPENROUTER_API_KEY is not set.")
        from openai import AsyncOpenAI, OpenAI

        headers = {"HTTP-Referer": s.base_url, "X-Title": s.app_name}
        common = dict(
            api_key=s.openrouter_api_key,
            base_url=s.openrouter_base_url,
            timeout=s.llm_timeout_seconds,
            max_retries=2,
            default_headers=headers,
        )
        self._client = OpenAI(**common)
        self._aclient = AsyncOpenAI(**common)

    def complete(self, model: str, messages: list[dict], json_mode: bool = False, temperature: float = 0.4) -> str:
        from openai import BadRequestError, OpenAIError

        if not model:
            raise LLMError("No model configured. Set OUTREACH_MODEL / INTERVIEW_MODEL in .env.")
        kwargs = {"model": model, "messages": messages, "temperature": temperature}
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
        try:
            try:
                resp = self._client.chat.completions.create(**kwargs)
            except BadRequestError:
                if not json_mode:
                    raise
                # Some models/providers reject response_format; the prompt still asks for JSON.
                kwargs.pop("response_format")
                resp = self._client.chat.completions.create(**kwargs)
        except OpenAIError as exc:
            raise LLMError(f"Model call failed ({type(exc).__name__}): {str(exc)[:300]}") from exc
        if not resp.choices:
            raise LLMError("Model returned no choices.")
        content = resp.choices[0].message.content or ""
        if not content.strip():
            raise LLMError("Model returned an empty response.")
        return content

    async def stream(self, model: str, messages: list[dict], temperature: float = 0.5) -> AsyncIterator[str]:
        from openai import OpenAIError

        if not model:
            raise LLMError("No model configured.")
        try:
            stream = await self._aclient.chat.completions.create(
                model=model, messages=messages, temperature=temperature, stream=True
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except OpenAIError as exc:
            raise LLMError(f"Model stream failed ({type(exc).__name__}): {str(exc)[:300]}") from exc


_llm: LLM | None = None


def get_llm() -> LLM:
    global _llm
    if _llm is None:
        _llm = OpenRouterLLM()
    return _llm


def set_llm(llm: LLM | None) -> None:
    """Override the model client (tests) or reset it (None)."""
    global _llm
    _llm = llm


# ---------- prompts ----------


def load_prompt(name: str, **values: str) -> str:
    """Load prompts/<name>.md and fill {{placeholders}}. Unknown placeholders are left as-is."""
    text = (PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(values.get(m.group(1), m.group(0))), text)


# ---------- structured output ----------


def extract_json(text: str) -> dict:
    """Pull a JSON object out of a model response (handles ```json fences and chatter)."""
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.DOTALL)
    if fence:
        t = fence.group(1)
    else:
        start, end = t.find("{"), t.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("No JSON object found in response")
        t = t[start : end + 1]
    data = json.loads(t)
    if not isinstance(data, dict):
        raise ValueError("Response JSON is not an object")
    return data


def complete_structured(
    model: str, messages: list[dict], schema: type[T], temperature: float = 0.3, retries: int = 1
) -> T:
    """Call the model in JSON mode and validate against a Pydantic schema.

    On invalid output, retries with the validation error fed back to the model.
    Raises LLMError if it still fails.
    """
    llm = get_llm()
    msgs = list(messages)
    last_error = ""
    for _ in range(retries + 1):
        raw = llm.complete(model, msgs, json_mode=True, temperature=temperature)
        try:
            return schema.model_validate(extract_json(raw))
        except (ValueError, ValidationError) as exc:
            last_error = str(exc)[:800]
            log.warning("Invalid structured output from %s: %s", model, last_error[:200])
            msgs = msgs + [
                {"role": "assistant", "content": raw},
                {
                    "role": "user",
                    "content": "Your previous reply was not valid. Error:\n"
                    f"{last_error}\nReply again with ONLY one JSON object matching the required format.",
                },
            ]
    raise LLMError(f"Model did not return valid structured output: {last_error[:300]}")

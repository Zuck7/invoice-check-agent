"""Vision backends: the part that actually talks to a model.

Everything that makes an extraction *correct* — the prompt, the schema, the
validation, the retry ladder — lives in ``vision.py`` and is shared. A backend
only turns (document bytes, instruction) into a JSON string, so swapping
providers cannot quietly change what counts as a valid invoice.

Two are supplied:

* :class:`AnthropicBackend` — Claude via the Anthropic SDK
* :class:`GeminiBackend` — Google Gemini via the ``google-genai`` SDK

Neither is imported until it is used, so the package installs and the tests run
with neither SDK present.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

ANTHROPIC_MODEL = "claude-opus-5"
GEMINI_MODEL = "gemini-2.5-pro"

#: Which backend to build when nothing is specified. Read at call time, not
#: import time, so a .env loaded during startup still takes effect.
def default_provider() -> str:
    return os.environ.get("INVOICE_AUDIT_VISION", "gemini")


class BackendError(RuntimeError):
    """The provider could not be reached, or declined. Never a flag."""


class VisionBackend(Protocol):
    name: str

    def transcribe(
        self, data: bytes, media_type: str, instruction: str, system: str, schema: dict
    ) -> str:
        """Return the model's JSON response as a string."""
        ...


# --------------------------------------------------------------------------
# Google Gemini
# --------------------------------------------------------------------------


def to_gemini_schema(schema: Any) -> Any:
    """Translate our JSON Schema into the dialect Gemini accepts.

    The one real incompatibility is nullable fields: we write
    ``{"type": ["string", "null"]}``, Gemini wants
    ``{"type": "string", "nullable": true}``. Left untranslated the whole
    request is rejected, so this is not cosmetic.
    """
    if isinstance(schema, list):
        return [to_gemini_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key == "type" and isinstance(value, list):
            concrete = [t for t in value if t != "null"]
            out["type"] = concrete[0] if concrete else "string"
            if "null" in value:
                out["nullable"] = True
        elif key == "additionalProperties":
            continue  # not part of Gemini's schema dialect
        else:
            out[key] = to_gemini_schema(value)
    return out


@dataclass
class GeminiBackend:
    """Google Gemini.

    The API key is read from ``GOOGLE_API_KEY`` or ``GEMINI_API_KEY`` by the
    SDK itself; pass ``api_key`` only to override that.
    """

    name: str = "gemini"
    model: str = GEMINI_MODEL
    api_key: str | None = None
    client: Any = None
    _types: Any = field(default=None, repr=False)

    def _ensure(self) -> None:
        if self.client is not None and self._types is not None:
            return
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:  # pragma: no cover - environment issue
            raise BackendError(
                "Gemini extraction needs the Google SDK: pip install google-genai"
            ) from exc
        self._types = types
        if self.client is None:
            key = self.api_key or os.environ.get("GOOGLE_API_KEY") or os.environ.get(
                "GEMINI_API_KEY"
            )
            if not key:
                raise BackendError(
                    "No Google API key. Set GOOGLE_API_KEY (or GEMINI_API_KEY), "
                    "or pass --google-api-key."
                )
            self.client = genai.Client(api_key=key)

    def transcribe(
        self, data: bytes, media_type: str, instruction: str, system: str, schema: dict
    ) -> str:
        self._ensure()
        types = self._types
        try:
            response = self.client.models.generate_content(
                model=self.model,
                contents=[
                    types.Part.from_bytes(data=data, mime_type=media_type),
                    instruction,
                ],
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    response_json_schema=to_gemini_schema(schema),
                    # Transcription, not composition: no room for creativity.
                    temperature=0,
                    max_output_tokens=32000,
                ),
            )
        except Exception as exc:
            raise BackendError(f"Gemini request failed: {exc}") from exc

        blocked = getattr(getattr(response, "prompt_feedback", None), "block_reason", None)
        if blocked:
            raise BackendError(
                f"Gemini declined to read this document ({blocked}). "
                "Route it to a human."
            )

        text = getattr(response, "text", None)
        if not text:
            raise BackendError("Gemini returned no content")
        return text


# --------------------------------------------------------------------------
# Anthropic Claude
# --------------------------------------------------------------------------


@dataclass
class AnthropicBackend:
    name: str = "anthropic"
    model: str = ANTHROPIC_MODEL
    client: Any = None

    def _ensure(self) -> None:
        if self.client is not None:
            return
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - environment issue
            raise BackendError(
                "Claude extraction needs the Anthropic SDK: pip install anthropic"
            ) from exc
        self.client = anthropic.Anthropic()

    def transcribe(
        self, data: bytes, media_type: str, instruction: str, system: str, schema: dict
    ) -> str:
        import base64

        self._ensure()
        payload = base64.standard_b64encode(data).decode("ascii")
        block = (
            {"type": "document", "source": {"type": "base64",
                                            "media_type": media_type, "data": payload}}
            if media_type == "application/pdf"
            else {"type": "image", "source": {"type": "base64",
                                              "media_type": media_type, "data": payload}}
        )
        try:
            with self.client.beta.messages.stream(
                model=self.model,
                max_tokens=64000,
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                thinking={"type": "adaptive"},
                system=system,
                output_config={"format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user",
                           "content": [block, {"type": "text", "text": instruction}]}],
            ) as stream:
                response = stream.get_final_message()
        except Exception as exc:
            raise BackendError(f"Claude request failed: {exc}") from exc

        if getattr(response, "stop_reason", None) == "refusal":
            details = getattr(response, "stop_details", None)
            raise BackendError(
                f"Claude declined to read this document "
                f"({getattr(details, 'category', None)}). Route it to a human."
            )

        text = next(
            (b.text for b in response.content if getattr(b, "type", None) == "text"),
            None,
        )
        if not text:
            raise BackendError("Claude returned no content")
        return text


# --------------------------------------------------------------------------


def make_backend(provider: str | None = None, **kwargs: Any) -> VisionBackend:
    provider = (provider or default_provider()).lower()
    if provider in ("gemini", "google"):
        return GeminiBackend(**kwargs)
    if provider in ("anthropic", "claude"):
        return AnthropicBackend(**{k: v for k, v in kwargs.items() if k != "api_key"})
    raise BackendError(
        f"unknown vision provider {provider!r} — use 'gemini' or 'anthropic'"
    )

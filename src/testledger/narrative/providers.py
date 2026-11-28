"""Provider clients for the narrative feature.

Two providers, both speaking the OpenAI chat-completions shape:

* ``ollama`` — any local OpenAI-compatible endpoint (Ollama, llama.cpp,
  LM Studio …) through plain HTTP via ``requests``. The base URL comes from
  ``narrative.endpoint``; a key is only sent when the configured env var is
  set (local servers usually need none).
* ``openai`` — the hosted OpenAI API through the optional ``openai`` package
  (``pip install testledger[narrative]``). The key comes only from the
  environment variable named by ``narrative.api_key_env``.
"""

from __future__ import annotations

import os

from testledger.config import Narrative
from testledger.narrative.generate import NarrativeError


def chat_completion(system: str, prompt: str, settings: Narrative) -> str:
    if settings.provider == "openai":
        try:
            import openai
        except ImportError as exc:
            raise NarrativeError(
                "the 'openai' package is not installed — run: pip install testledger[narrative]"
            ) from exc
        api_key = os.environ.get(settings.api_key_env)
        if not api_key:
            raise NarrativeError(f"${settings.api_key_env} is not set")
        client = openai.OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model=settings.model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            timeout=settings.timeout_s,
        )
        return resp.choices[0].message.content or ""

    if settings.provider in ("ollama", "openai-compatible"):
        import requests

        headers = {"Content-Type": "application/json"}
        api_key = os.environ.get(settings.api_key_env)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        resp = requests.post(
            f"{settings.endpoint.rstrip('/')}/chat/completions",
            json={
                "model": settings.model,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt},
                ],
                "stream": False,
            },
            headers=headers,
            timeout=settings.timeout_s,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    raise NarrativeError(f"unknown narrative provider {settings.provider!r} (expected 'ollama' or 'openai')")

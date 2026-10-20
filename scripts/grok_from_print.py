"""One-off: drive from-print with Grok instead of Gemma. Same harness."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

AUTH = Path.home() / ".grok" / "auth.json"
PROXY = "https://cli-chat-proxy.grok.com/v1/chat/completions"
MODEL = "grok-4.6"


def _bearer() -> str:
    data = json.loads(AUTH.read_text(encoding="utf-8"))
    rec = next(iter(data.values()))
    key = str(rec.get("key") or "")
    if not key:
        raise RuntimeError("no Grok session token in ~/.grok/auth.json")
    return key


class GrokClient:
    def __init__(self, model: str = MODEL, images: list[str] | None = None, timeout: float = 180.0) -> None:
        self.model = model
        self.images = list(images or [])
        self.timeout = timeout
        self.token = _bearer()

    def complete(self, messages: list[dict[str, str]]) -> str:
        payload_messages: list[dict[str, Any]] = []
        attached = False
        for message in messages:
            role = message["role"]
            text = message["content"]
            if role == "user" and self.images and not attached:
                url = "data:image/png;base64," + self.images[0]
                payload_messages.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": text},
                            {"type": "image_url", "image_url": {"url": url}},
                        ],
                    }
                )
                attached = True
            else:
                payload_messages.append({"role": role, "content": text})
        body = json.dumps(
            {
                "model": self.model,
                "messages": payload_messages,
                "temperature": 0.0,
                "stream": True,
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            PROXY,
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + self.token,
                "X-XAI-Token-Auth": "xai-grok-cli",
                "x-grok-model-override": self.model,
                "x-grok-client-identifier": "grok-shell",
                "x-grok-client-version": "1.0.5",
                "User-Agent": "grok/1.0.5",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                chunks: list[str] = []
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    delta = ((event.get("choices") or [{}])[0].get("delta") or {})
                    piece = delta.get("content")
                    if piece:
                        chunks.append(str(piece))
                if chunks:
                    return "".join(chunks)
                return ""
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:800]
            raise RuntimeError(f"Grok HTTP {exc.code}: {detail}") from exc


def main() -> None:
    from panelcad.sharpen.from_print import run_from_print

    pdf = Path(os.environ.get("USERPROFILE", "")) / ".panelcad" / "prints" / "wiring-motor-starter.pdf"
    result = run_from_print(
        pdf=pdf,
        page=1,
        model=MODEL,
        via_cli=False,
        client=GrokClient(model=MODEL),
    )
    print("ok" if result.ok else "FAIL", result.reason)
    print("job", result.job)
    print("turns", len(result.turns), "s", round(result.elapsed_s, 1))


if __name__ == "__main__":
    main()

"""Extract a readable reason from an already redacted upstream response."""

import json


def upstream_message(excerpt: str) -> str:
    text = (excerpt or "").strip()
    start = text.find("{")
    if start >= 0:
        try:
            payload = json.loads(text[start:])
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            message = error.get("message") if isinstance(error, dict) else error
            if not isinstance(message, str) or not message.strip():
                message = payload.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()[:512]
    return text[:512]

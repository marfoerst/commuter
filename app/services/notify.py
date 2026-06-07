"""Proactive push delivery (opt-in).

Two zero-dependency sinks: an ntfy topic (https://ntfy.sh or self-hosted) and a
generic JSON webhook. Both are best-effort — a delivery failure is logged and
never propagates, so a flaky notifier can't break the dashboard or the
scheduler. Until a bridge reopens, "leave now / it's bad today" is the single
most useful thing this app can tell you without you opening it; this is that.
"""

from __future__ import annotations

import logging

import httpx

from app.config import NTFY_TOPIC_URL, PUSH_MIN_SEVERITY, WEBHOOK_URL

log = logging.getLogger(__name__)

SEVERITY_RANK = {"clear": 0, "watch": 1, "alert": 2}


def push_enabled() -> bool:
    return bool(NTFY_TOPIC_URL or WEBHOOK_URL)


def meets_threshold(severity: str) -> bool:
    threshold = SEVERITY_RANK.get(PUSH_MIN_SEVERITY, 2)
    return SEVERITY_RANK.get(severity, 0) >= threshold


async def send_push(
    client: httpx.AsyncClient,
    title: str,
    message: str,
    severity: str,
    data: dict | None = None,
) -> None:
    if NTFY_TOPIC_URL:
        priority = "urgent" if severity == "alert" else "high"
        tags = "rotating_light" if severity == "alert" else "warning"
        try:
            await client.post(
                NTFY_TOPIC_URL,
                content=message.encode("utf-8"),
                headers={"Title": title, "Priority": priority, "Tags": tags},
                timeout=15.0,
            )
        except Exception as e:  # noqa: BLE001 - best-effort
            log.warning("ntfy push failed: %s", e)

    if WEBHOOK_URL:
        payload = {"title": title, "message": message, "severity": severity}
        if data:
            payload.update(data)
        try:
            await client.post(WEBHOOK_URL, json=payload, timeout=15.0)
        except Exception as e:  # noqa: BLE001 - best-effort
            log.warning("webhook push failed: %s", e)

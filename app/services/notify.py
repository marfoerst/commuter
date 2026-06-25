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

log = logging.getLogger(__name__)

SEVERITY_RANK = {"clear": 0, "watch": 1, "alert": 2}


def user_push_enabled(user: dict) -> bool:
    """True if this user has at least one push sink configured."""
    return bool(user.get("ntfy_topic_url") or user.get("webhook_url"))


def meets_threshold(severity: str, min_severity: str = "alert") -> bool:
    threshold = SEVERITY_RANK.get(min_severity, 2)
    return SEVERITY_RANK.get(severity, 0) >= threshold


async def send_push(
    client: httpx.AsyncClient,
    user: dict,
    title: str,
    message: str,
    severity: str,
    data: dict | None = None,
) -> None:
    """Best-effort push to a single user's configured sinks (ntfy / webhook)."""
    ntfy_url = user.get("ntfy_topic_url")
    webhook_url = user.get("webhook_url")

    if ntfy_url:
        priority = "urgent" if severity == "alert" else "high"
        tags = "rotating_light" if severity == "alert" else "warning"
        try:
            await client.post(
                ntfy_url,
                content=message.encode("utf-8"),
                headers={"Title": title, "Priority": priority, "Tags": tags},
                timeout=15.0,
            )
        except Exception as e:  # noqa: BLE001 - best-effort
            log.warning("ntfy push failed for user %s: %s", user.get("id"), e)

    if webhook_url:
        payload = {"title": title, "message": message, "severity": severity}
        if data:
            payload.update(data)
        try:
            await client.post(webhook_url, json=payload, timeout=15.0)
        except Exception as e:  # noqa: BLE001 - best-effort
            log.warning("webhook push failed for user %s: %s", user.get("id"), e)

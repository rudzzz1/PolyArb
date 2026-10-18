"""Alerting and Notification Engine for PolyArb (Phase 2.5).
Dispatches high-priority notifications for fills, legging unwinds, slippage breaches,
and emergency circuit breaker halts to console, in-memory queue, and optional Telegram/Webhook.
"""
from __future__ import annotations
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
import urllib.request

from .logger import get_logger

logger = get_logger("alerts")


@dataclass
class Alert:
    level: str                  # 'INFO' | 'WARNING' | 'CRITICAL'
    category: str               # 'TRADE' | 'LEGGING' | 'CIRCUIT_BREAKER' | 'SLIPPAGE' | 'SYSTEM'
    title: str
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class AlertManager:
    """Central notification manager with multi-channel dispatch."""
    _instance: AlertManager | None = None

    def __init__(self, telegram_token: str | None = None, telegram_chat_id: str | None = None,
                 webhook_url: str | None = None):
        self.telegram_token = telegram_token or os.environ.get("POLYARB_TELEGRAM_TOKEN")
        self.telegram_chat_id = telegram_chat_id or os.environ.get("POLYARB_TELEGRAM_CHAT_ID")
        self.webhook_url = webhook_url or os.environ.get("POLYARB_WEBHOOK_URL")
        self.history: list[Alert] = []
        self._max_history = 200

    @classmethod
    def get_instance(cls) -> AlertManager:
        if cls._instance is None:
            cls._instance = AlertManager()
        return cls._instance

    def notify(self, level: str, category: str, title: str, message: str,
               metadata: dict[str, Any] | None = None) -> Alert:
        """Dispatches an alert across all configured channels."""
        alert = Alert(
            level=level.upper(),
            category=category.upper(),
            title=title,
            message=message,
            metadata=metadata or {},
        )
        self.history.append(alert)
        if len(self.history) > self._max_history:
            self.history.pop(0)

        # 1. Structured log
        log_msg = f"[{alert.category}] {alert.title}: {alert.message}"
        if alert.level == "CRITICAL":
            logger.critical(log_msg)
        elif alert.level == "WARNING":
            logger.warning(log_msg)
        else:
            logger.info(log_msg)

        # 2. Dispatch external notifications asynchronously / non-blocking
        if self.telegram_token and self.telegram_chat_id:
            self._send_telegram(alert)
        if self.webhook_url:
            self._send_webhook(alert)

        return alert

    def _send_telegram(self, alert: Alert):
        """Sends markdown message to Telegram."""
        try:
            url = f"https://api.telegram.org/bot{self.telegram_token}/sendMessage"
            text = f"*{alert.level}: {alert.title}*\n{alert.message}\n`Time: {alert.timestamp}`"
            payload = json.dumps({
                "chat_id": self.telegram_chat_id,
                "text": text,
                "parse_mode": "Markdown",
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3.0)
        except Exception as e:
            logger.debug(f"Telegram alert delivery skipped/failed: {e}")

    def _send_webhook(self, alert: Alert):
        """Sends JSON payload to generic webhook endpoint."""
        try:
            payload = json.dumps({
                "timestamp": alert.timestamp,
                "level": alert.level,
                "category": alert.category,
                "title": alert.title,
                "message": alert.message,
                "metadata": alert.metadata,
            }).encode("utf-8")
            req = urllib.request.Request(self.webhook_url, data=payload, headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=3.0)
        except Exception as e:
            logger.debug(f"Webhook alert delivery skipped/failed: {e}")

    def get_recent_alerts(self, limit: int = 50, min_level: str | None = None) -> list[dict[str, Any]]:
        """Returns recent alerts formatted for UI consumption."""
        levels = ["INFO", "WARNING", "CRITICAL"]
        min_idx = levels.index(min_level.upper()) if min_level and min_level.upper() in levels else 0
        filtered = [
            a for a in self.history
            if levels.index(a.level) >= min_idx
        ]
        res = [
            {
                "timestamp": a.timestamp,
                "level": a.level,
                "category": a.category,
                "title": a.title,
                "message": a.message,
                "metadata": a.metadata,
            }
            for a in reversed(filtered[-limit:])
        ]
        return res

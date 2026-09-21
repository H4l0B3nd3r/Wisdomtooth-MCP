"""What each connected account has left, from the sources that actually say.

- The Claude subscription reports its own meter: Claude Code streams a
  `rate_limit_event` whose `unifiedWindows` give the 5-hour and 7-day
  utilization (0.0-1.0) and when each resets. That is a share of the plan,
  not a token count, so the meter also learns what a percentage point costs:
  each consult's API-equivalent cost set against how far it moved the meter.
  Anything else using the plan at the same time inflates that figure, which
  errs towards asking the user rather than overspending.
- OpenRouter reports a key's remaining credit at GET /key.
- Most hosted APIs send `x-ratelimit-*` headers: shown, never used to hold a
  consult, because they refill within a minute.
- Allowances the user declares are measured against the usage ledger by the
  server, not here.

The last reading per advisor is kept in a small JSON file, so a restarted
server still knows where the plan stood.
"""

import json
import os
import threading
import time
from typing import Callable, Optional

# unifiedWindows keys -> the labels used everywhere else.
PLAN_WINDOWS = (("five_hour", "5 h"), ("seven_day", "7 d"))
# Readings of the same window agree on its reset time to within this.
_SAME_WINDOW_S = 120
# A credit reading older than this is refetched before it gates a consult.
CREDIT_MAX_AGE_S = 60


class Meter:
    def __init__(self, path: Callable[[], str]):
        self._path = path
        self._lock = threading.Lock()
        self._data: Optional[dict] = None

    # -- storage -----------------------------------------------------------

    def _load(self) -> dict:
        if self._data is None:
            try:
                with open(self._path(), encoding="utf-8") as fh:
                    data = json.load(fh)
                self._data = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def _save(self) -> None:
        path = self._path()
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, separators=(",", ":"))
            os.replace(tmp, path)
        except OSError:
            pass  # monitoring must never cost the user an answer

    def _entry(self, advisor: str) -> dict:
        return self._load().setdefault(advisor, {})

    # -- recording ---------------------------------------------------------

    def note_plan(self, advisor: str, info: dict, cost_usd) -> None:
        """Record a `rate_limit_info` and learn from how far it moved."""
        now = time.time()
        with self._lock:
            entry = self._entry(advisor)
            windows = info.get("unifiedWindows")
            windows = windows if isinstance(windows, dict) else {}
            last = entry.setdefault("last", {})
            rates = entry.setdefault("rates", {})
            for key, _label in PLAN_WINDOWS:
                window = windows.get(key)
                if not isinstance(window, dict):
                    continue
                try:
                    util = float(window.get("utilization"))
                    resets = float(window.get("resetsAt"))
                except (TypeError, ValueError):
                    continue
                prev = last.get(key)
                if (prev and abs(float(prev["resets"]) - resets) < _SAME_WINDOW_S
                        and isinstance(cost_usd, (int, float)) and cost_usd > 0
                        and util >= float(prev["util"])):
                    rate = rates.setdefault(key, {"delta": 0.0, "usd": 0.0})
                    rate["delta"] += util - float(prev["util"])
                    rate["usd"] += float(cost_usd)
                last[key] = {"util": util, "resets": resets}
            entry["plan"] = {"status": info.get("status"),
                             "overage": info.get("isUsingOverage"),
                             "seen": now}
            self._save()

    def note_headers(self, advisor: str, headers: dict) -> None:
        def number(name):
            try:
                return int(float(headers[name]))
            except (KeyError, TypeError, ValueError):
                return None
        limit = number("x-ratelimit-limit-tokens")
        left = number("x-ratelimit-remaining-tokens")
        if limit is None and left is None:
            return
        with self._lock:
            self._entry(advisor)["ratelimit"] = {
                "limit": limit, "remaining": left,
                "reset": headers.get("x-ratelimit-reset-tokens"),
                "seen": time.time()}
            self._save()

    def note_credit(self, advisor: str, data: dict) -> None:
        def number(name):
            value = data.get(name)
            return float(value) if isinstance(value, (int, float)) else None
        with self._lock:
            self._entry(advisor)["credit"] = {
                "limit": number("limit"), "remaining": number("limit_remaining"),
                "usage": number("usage"), "seen": time.time()}
            self._save()

    # -- reading -----------------------------------------------------------

    def plan_windows(self, advisor: str, now: Optional[float] = None) -> list:
        """[(label, utilization, resets_at)] for windows that have not reset."""
        now = time.time() if now is None else now
        with self._lock:
            last = self._load().get(advisor, {}).get("last", {})
            return [(label, float(last[key]["util"]), float(last[key]["resets"]))
                    for key, label in PLAN_WINDOWS
                    if key in last and float(last[key]["resets"]) > now]

    def plan_status(self, advisor: str) -> dict:
        with self._lock:
            return dict(self._load().get(advisor, {}).get("plan", {}))

    def plan_remaining_usd(self, advisor: str,
                           now: Optional[float] = None) -> Optional[float]:
        """API-equivalent dollars left before the tightest window is full.

        0.0 for a full window whatever the rate; None when no window has a
        learned cost per point, because then there is no evidence either way.
        """
        now = time.time() if now is None else now
        with self._lock:
            entry = self._load().get(advisor, {})
            last, rates = entry.get("last", {}), entry.get("rates", {})
            best = None
            for key, _label in PLAN_WINDOWS:
                window = last.get(key)
                if not window or float(window["resets"]) <= now:
                    continue
                left = max(0.0, 1.0 - float(window["util"]))
                if left <= 0:
                    return 0.0
                rate = rates.get(key) or {}
                if rate.get("delta", 0) > 0 and rate.get("usd", 0) > 0:
                    dollars = left / (rate["delta"] / rate["usd"])
                    best = dollars if best is None else min(best, dollars)
            return best

    def ratelimit(self, advisor: str) -> dict:
        with self._lock:
            return dict(self._load().get(advisor, {}).get("ratelimit", {}))

    def credit(self, advisor: str) -> dict:
        with self._lock:
            return dict(self._load().get(advisor, {}).get("credit", {}))

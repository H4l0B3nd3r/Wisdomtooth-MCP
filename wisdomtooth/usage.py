"""Usage accounting: token counts, API-rate cost estimates, the ledger file,
consult caps, and the reports built from them.

Every consult is logged to a local JSON-lines ledger with the tokens it used,
its API-equivalent cost and how long it took. The server cannot see the plan's
own 5-hour and weekly meters, so this is the user's best view of what the
advisor spends against them -- and what the optional caps count.
"""

import json
import os
import time
from typing import Optional

# USD per million tokens (input, output) at first-party API rates, matched by
# longest prefix. Used only to *estimate*: a subscription consult is not billed
# per token, and when the CLI reports its own API-equivalent figure that wins.
# An unlisted model gets no estimate rather than a wrong one.
PRICES_PER_MTOK = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_PRICE_PREFIXES = sorted(PRICES_PER_MTOK, key=len, reverse=True)
CACHE_READ_FACTOR = 0.1    # cache hits bill at ~0.1x the input rate
CACHE_WRITE_FACTOR = 1.25  # 5-minute cache writes at ~1.25x

LEDGER_MAX_BYTES = 2_000_000
LEDGER_KEEP_DAYS = 35
WINDOWS = (("1 h", 3600), ("5 h", 5 * 3600), ("24 h", 86400),
           ("7 d", 7 * 86400))


def estimate_cost(model: str, input_tokens: int = 0, output_tokens: int = 0,
                  cache_read: int = 0, cache_write: int = 0) -> Optional[float]:
    for prefix in _PRICE_PREFIXES:
        if model.startswith(prefix):
            rate_in, rate_out = PRICES_PER_MTOK[prefix]
            return round((input_tokens * rate_in
                          + cache_read * rate_in * CACHE_READ_FACTOR
                          + cache_write * rate_in * CACHE_WRITE_FACTOR
                          + output_tokens * rate_out) / 1_000_000, 6)
    return None


def cli_usage(payload: dict, model: str) -> dict:
    """Tokens and cost from the CLI's `result` object (`json` or the last
    line of `stream-json`).

    `modelUsage` is preferred over `usage`: Claude Code's cost-tracking docs
    note that `usage` can under-report on some error results, while
    `modelUsage` and `total_cost_usd` keep the full figure.
    """
    per_model = payload.get("modelUsage")
    if isinstance(per_model, dict) and per_model:
        def total(key):
            return sum(int(v.get(key) or 0) for v in per_model.values()
                       if isinstance(v, dict))
        tokens = dict(input_tokens=total("inputTokens"),
                      output_tokens=total("outputTokens"),
                      cache_read_tokens=total("cacheReadInputTokens"),
                      cache_write_tokens=total("cacheCreationInputTokens"))
    else:
        usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else {}
        tokens = dict(
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            cache_read_tokens=int(usage.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(usage.get("cache_creation_input_tokens") or 0))
    try:
        cost = float(payload["total_cost_usd"])
        source = "cli"
    except (KeyError, TypeError, ValueError):
        cost = estimate_cost(model, tokens["input_tokens"],
                             tokens["output_tokens"],
                             tokens["cache_read_tokens"],
                             tokens["cache_write_tokens"])
        source = "estimate" if cost is not None else None
    return dict(tokens, cost_usd=cost, cost_source=source)


def api_usage(message, model: str) -> dict:
    """Tokens from a Messages API response, costed at list prices."""
    usage = getattr(message, "usage", None)
    if usage is None:
        return {}

    def get(name):
        try:
            return int(getattr(usage, name, 0) or 0)
        except (TypeError, ValueError):
            return 0
    tokens = dict(input_tokens=get("input_tokens"),
                  output_tokens=get("output_tokens"),
                  cache_read_tokens=get("cache_read_input_tokens"),
                  cache_write_tokens=get("cache_creation_input_tokens"))
    served_by = getattr(message, "model", None)
    cost = estimate_cost(served_by if isinstance(served_by, str) else model,
                         tokens["input_tokens"], tokens["output_tokens"],
                         tokens["cache_read_tokens"], tokens["cache_write_tokens"])
    return dict(tokens, cost_usd=cost,
                cost_source="estimate" if cost is not None else None)


def usage_record(kind: str, backend: str, model: str, effort: Optional[str],
                 status: str, user_content: str, **extra) -> dict:
    return dict(ts=round(time.time(), 3), time=time.strftime("%Y-%m-%dT%H:%M:%S"),
                kind=kind, backend=backend, model=model, effort=effort,
                status=status, prompt_chars=len(user_content), **extra)


def read_ledger(path: str) -> list:
    records = []
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue  # a torn line from a crash costs one record
                if isinstance(record, dict):
                    records.append(record)
    except OSError:
        pass
    return records


def compact_ledger(path: str) -> None:
    cutoff = time.time() - LEDGER_KEEP_DAYS * 86400
    keep = [r for r in read_ledger(path) if float(r.get("ts", 0)) >= cutoff]
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for record in keep:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
    os.replace(tmp, path)


def billed(records: list) -> list:
    """Records whose tokens were spent: successes, and failures the backend
    still reported usage for (a CLI run can fail after the model ran)."""
    return [r for r in records if r.get("status") in ("ok", "error")]


def fmt_usd(cost: float) -> str:
    """Cents, or enough digits that a sub-cent consult does not read as free."""
    return f"${cost:.2f}" if cost >= 0.01 or not cost else f"${cost:.4f}"


def fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 10_000:
        return f"{n / 1000:.0f}k"
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def summarise(records: list) -> dict:
    ok = [r for r in records if r.get("status") == "ok"]
    spent = billed(records)
    costs = [float(r["cost_usd"]) for r in spent
             if isinstance(r.get("cost_usd"), (int, float))]
    times = [float(r["duration_s"]) for r in ok
             if isinstance(r.get("duration_s"), (int, float))]
    return {
        "consults": len(ok),
        "repeats": sum(1 for r in records if r.get("status") == "repeat"),
        "errors": sum(1 for r in records if r.get("status") == "error"),
        "input": sum(int(r.get("input_tokens") or 0)
                     + int(r.get("cache_read_tokens") or 0)
                     + int(r.get("cache_write_tokens") or 0) for r in spent),
        "output": sum(int(r.get("output_tokens") or 0) for r in spent),
        "cost": round(sum(costs), 4) if costs else None,
        "avg_s": sum(times) / len(times) if times else None,
    }


def usage_footer(record: dict) -> str:
    """One line for the answer footer: what this consult used."""
    tokens_in = sum(int(record.get(k) or 0) for k in
                    ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
    tokens_out = int(record.get("output_tokens") or 0)
    if not (tokens_in or tokens_out):
        return ""
    text = (f"\n[usage: {tokens_in:,} tokens in · {tokens_out:,} out · "
            f"{record.get('duration_s', 0):g}s")
    cost = record.get("cost_usd")
    if isinstance(cost, (int, float)):
        text += f" · ≈{fmt_usd(cost)} at API rates"
    return text + "]"


def cap_violation(records: list, now: float, caps, max_usd_per_day: float
                  ) -> Optional[str]:
    """The refusal message for the first cap `records` would break, if any.

    `caps` is a sequence of (span_seconds, cap, env_var, label); a cap of 0 is
    off. Only consults that reached the model count.
    """
    ok = [r for r in records if r.get("status") == "ok"]
    for span, cap, var, label in caps:
        if not cap:
            continue
        inside = sorted(float(r["ts"]) for r in ok
                        if float(r["ts"]) >= now - span)
        if len(inside) >= cap:
            opens = time.strftime(
                "%H:%M", time.localtime(inside[len(inside) - cap] + span))
            return (f"Consult cap reached: {len(inside)} consults in the last "
                    f"{label}, and the limit is {cap} ({var}). No consult was "
                    f"made and nothing was spent. The next slot opens around "
                    f"{opens}. Tell the user; do NOT retry. They can raise or "
                    f"unset {var}.")
    if max_usd_per_day:
        spent = sum(float(r.get("cost_usd") or 0) for r in billed(records)
                    if float(r["ts"]) >= now - 86400)
        if spent >= max_usd_per_day:
            return (f"Daily spend cap reached: consults in the last 24 hours "
                    f"come to ≈{fmt_usd(spent)} at API rates, and the limit is "
                    f"${max_usd_per_day:.2f} (ADVISOR_MAX_USD_PER_DAY). No "
                    "consult was made. Tell the user; do NOT retry.")
    return None


def status_line(records: list, now: float) -> str:
    """The one-line summary advisor_status shows."""
    parts = []
    for label, span in (("5 h", 5 * 3600), ("7 d", 7 * 86400)):
        s = summarise([r for r in records if float(r["ts"]) >= now - span])
        parts.append(f"{label}: {s['consults']} consults, "
                     f"{fmt_tokens(s['input'])} in / {fmt_tokens(s['output'])} out"
                     + (f", ≈{fmt_usd(s['cost'])}" if s["cost"] is not None
                        else ""))
    return "; ".join(parts) + " (API-rate estimate)"


def report(records: list, now: float, days: int, where: str, caps_text: str,
           repeat_text: str, sections: Optional[list] = None) -> str:
    """The advisor_usage table."""
    lines = [f"USAGE LEDGER: {where}", "",
             f"{'window':<8}{'consults':>9}{'repeats':>9}{'errors':>8}"
             f"{'tokens in':>11}{'tokens out':>12}{'≈ cost':>10}{'avg time':>10}"]
    for label, span in WINDOWS:
        s = summarise([r for r in records if float(r["ts"]) >= now - span])
        cost = fmt_usd(s["cost"]) if s["cost"] is not None else "-"
        avg = f"{s['avg_s']:.0f}s" if s["avg_s"] is not None else "-"
        lines.append(f"{label:<8}{s['consults']:>9}{s['repeats']:>9}"
                     f"{s['errors']:>8}{fmt_tokens(s['input']):>11}"
                     f"{fmt_tokens(s['output']):>12}{cost:>10}{avg:>10}")
    by_model: dict = {}
    for r in records:
        if float(r["ts"]) >= now - days * 86400 and r.get("status") == "ok":
            by_model.setdefault(r.get("model") or "?", []).append(r)
    lines += ["", f"BY MODEL, last {days} day(s):"]
    if not by_model:
        lines.append("  (no consults)")
    for model, rows in sorted(by_model.items()):
        s = summarise(rows)
        lines.append(f"  {model:<22} {s['consults']:>4} consults  "
                     f"{fmt_tokens(s['input'])} in / {fmt_tokens(s['output'])} out"
                     + (f"  ≈{fmt_usd(s['cost'])}" if s["cost"] is not None
                        else ""))
    if sections:
        lines += [""] + list(sections)
    lines += [
        "",
        "CAPS: " + caps_text,
        "REPEAT GUARD: " + repeat_text,
        "",
        "Cost is what these tokens would cost at API rates. Subscription "
        "consults are not billed per token, but this is a fair proxy for how "
        "much of the plan's 5-hour and weekly allowance they use. The plan's "
        "own meter, when Claude Code reports it, is under BALANCES."]
    return "\n".join(lines)

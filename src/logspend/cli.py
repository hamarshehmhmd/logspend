"""logspend command line."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import List, Optional

from . import __version__
from .mine import LEVEL_RANK, PRICING, mine, monthly_cost, suggest
from .parse import read_records
from .rules import DEFAULT_FIELD, TARGETS, render


def _color_on(stream) -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    return hasattr(stream, "isatty") and stream.isatty()


class C:
    on = False

    @classmethod
    def w(cls, s, code):
        return f"\x1b[{code}m{s}\x1b[0m" if cls.on else s


def parse_window(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", s)
    if not m:
        raise argparse.ArgumentTypeError(f"bad window {s!r}: use e.g. 90s, 15m, 1h, 7d")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000 or unit == "TB":
            return f"{n:,.0f} {unit}" if unit == "B" else f"{n:,.1f} {unit}"
        n /= 1000.0
    return f"{n} B"


def human_window(s: float) -> str:
    if s >= 86400:
        return f"{s / 86400:.1f} days"
    if s >= 3600:
        return f"{s / 3600:.1f} h"
    if s >= 60:
        return f"{s / 60:.1f} min"
    return f"{s:.0f} s"


def _pricing(args):
    base = dict(PRICING[args.vendor])
    if args.price_gb is not None:
        base["per_gb"] = args.price_gb
    if args.price_per_million is not None:
        base["per_million"] = args.price_per_million
    if args.price_gb is not None or args.price_per_million is not None:
        base["note"] = f"custom: ${base['per_gb']}/GB + ${base['per_million']}/M events"
    base["name"] = args.vendor
    return base


def _after(p) -> float:
    if p.action == "drop":
        return 0.0
    if p.action == "sample":
        return 1.0 / max(1, p.sample_keep)
    return 1.0


def cmd_analyze(args) -> int:
    patterns, detected, total = mine(read_records(args.files, args.message_key), args.merge_threshold)
    if total == 0:
        print("logspend: no log lines read", file=sys.stderr)
        return 2
    window = args.window or detected
    window_note = "from --window" if args.window else "from timestamps"
    if not window:
        window, window_note = 86400.0, "ASSUMED 24 h: no timestamps found, pass --window"
    price = _pricing(args)
    suggest(patterns, args.min_share, args.min_count, args.sample_keep)
    tb = sum(p.bytes for p in patterns)
    for p in patterns:
        p.cost = monthly_cost(p.bytes * args.scale, p.count * args.scale, window, price["per_gb"], price["per_million"])
    patterns.sort(key=lambda p: (-p.cost, -p.bytes, p.template))
    for i, p in enumerate(patterns, 1):
        p.id = f"p{i}"
    cost_now = sum(p.cost for p in patterns)
    cost_after = sum(p.cost * _after(p) for p in patterns)
    bytes_after = sum(p.bytes * _after(p) for p in patterns)

    plan = {
        "logspend": __version__,
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sources": args.files,
        "lines": total,
        "bytes": tb,
        "window_seconds": window,
        "window_note": window_note,
        "scale": args.scale,
        "pricing": price,
        "monthly_cost_now": round(cost_now, 2),
        "monthly_cost_after": round(cost_after, 2),
        "patterns": [
            {
                "id": p.id, "action": p.action, "sample_keep": p.sample_keep or None, "reason": p.reason,
                "template": p.template, "regex": p.regex, "level": p.level, "max_level": p.max_level,
                "count": p.count, "bytes": p.bytes, "share": round(p.bytes / tb, 4),
                "monthly_cost": round(p.cost, 2), "services": dict(p.services), "example": p.example,
            }
            for p in patterns
        ],
    }
    if args.plan:
        with open(args.plan, "w", encoding="utf-8") as fh:
            json.dump(plan, fh, indent=2)
            fh.write("\n")
    if args.json:
        json.dump(plan, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    w = C.w
    print(w(f"logspend {__version__}", "1") + f"  {total:,} lines, {human_bytes(tb)} over {human_window(window)} ({window_note})")
    print(f"pricing: {price['note']} (approximate list price; override with --price-gb)"
          + (f", scaled x{args.scale:g}" if args.scale != 1 else ""))
    print(f"{len(patterns)} patterns, sorted by cost. Projected monthly bill for these sources: " + w(f"${cost_now:,.2f}", "1"))
    print()
    print(w(f"{'id':<5}{'bytes%':>6} {'$/month':>10} {'lines':>9} {'level':<6} {'action':<11} template", "2"))
    shown = patterns[: args.top]
    for p in shown:
        act = p.action if p.action != "sample" else f"sample/{p.sample_keep}"
        colr = {"drop": "31", "sample": "33"}.get(p.action, "32")
        t = p.template if len(p.template) <= 70 else p.template[:67] + "..."
        print(f"{p.id:<5}{p.bytes / tb:>6.1%} {p.cost:>10,.2f} {p.count:>9,} {p.level:<6} " + w(f"{act:<11}", colr) + " " + t)
    if len(patterns) > len(shown):
        rest = patterns[len(shown):]
        print(w(f"... {len(rest)} more patterns, {sum(p.bytes for p in rest) / tb:.1%} of bytes (--top N to show)", "2"))
    print()
    acts = [p for p in patterns if p.action != "keep"]
    if acts:
        print(w("Suggested:", "1"))
        for p in acts:
            print(f"  {p.id}: {p.action:<6} {p.reason}")
    big = [p for p in patterns if p.count and p.bytes / p.count > 1024 and p.bytes / tb > 0.02]
    for p in big:
        print(f"  {p.id}: note   avg {human_bytes(p.bytes / p.count)} per line: trim payloads / stack dumps at the source")
    saved = cost_now - cost_after
    pct = (saved / cost_now) if cost_now else 0
    print()
    print(f"With these suggestions: {human_bytes(tb)} -> {human_bytes(bytes_after)} in this sample, "
          + w(f"${cost_now:,.2f} -> ${cost_after:,.2f}/month (-{pct:.0%})", "1;32"))
    print(w("Warn/error lines and anything that mentions a failure are never dropped or sampled.", "2"))
    if args.plan:
        print(f"Plan written to {args.plan}. Edit actions there, then: logspend rules {args.plan} --target otel|vector|fluentbit")
    else:
        print("Write a plan with --plan plan.json, then: logspend rules plan.json --target otel|vector|fluentbit")
    return 0


def load_plan(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        plan = json.load(fh)
    if "patterns" not in plan:
        raise SystemExit(f"logspend: {path} is not a logspend plan")
    for p in plan["patterns"]:
        if p.get("action") not in ("keep", "drop", "sample"):
            raise SystemExit(f"logspend: {p.get('id')}: action must be keep, drop or sample")
        re.compile(p["regex"])
    return plan


def cmd_rules(args) -> int:
    plan = load_plan(args.plan)
    text = render(plan, args.target, args.field, args.inputs)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(text)
    return 0


def cmd_simulate(args) -> int:
    plan = load_plan(args.plan)
    rules = [(p["id"], p["action"], int(p.get("sample_keep") or 10), re.compile(p["regex"]))
             for p in plan["patterns"] if p["action"] in ("drop", "sample")]
    window = plan.get("window_seconds") or 86400
    price = plan.get("pricing", PRICING["generic"])
    scale = float(plan.get("scale") or 1.0)
    lines = total_b = kept_b = 0
    kept_n = 0.0
    hits = {rid: 0 for rid, *_ in rules}
    risky = {}
    for r in read_records(args.files, None):
        lines += 1
        total_b += r.size
        frac = 1.0
        for rid, action, keep, rx in rules:
            if rx.search(r.raw):
                hits[rid] += 1
                frac = 0.0 if action == "drop" else 1.0 / keep
                if LEVEL_RANK.get(r.level, 2) >= 3:
                    risky.setdefault(rid, []).append(r.raw[:160])
                break
        kept_b += r.size * frac
        kept_n += frac
    if lines == 0:
        print("logspend: no log lines read", file=sys.stderr)
        return 2
    before = monthly_cost(total_b * scale, lines * scale, window, price["per_gb"], price["per_million"])
    after = monthly_cost(kept_b * scale, kept_n * scale, window, price["per_gb"], price["per_million"])
    result = {"lines": lines, "bytes_before": total_b, "bytes_after": round(kept_b),
              "monthly_cost_before": round(before, 2), "monthly_cost_after": round(after, 2),
              "rule_hits": hits, "warn_error_lines_hit": {k: len(v) for k, v in risky.items()},
              "unused_rules": [k for k, v in hits.items() if v == 0]}
    if args.json:
        json.dump(result, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        w = C.w
        print(w("logspend simulate", "1") + f"  {lines:,} lines, {len(rules)} rules from {args.plan}")
        for rid, n in hits.items():
            print(f"  {rid}: matched {n:,} lines" + (w("  (unused)", "33") if n == 0 else ""))
        print(f"bytes: {human_bytes(total_b)} -> {human_bytes(kept_b)} ({1 - kept_b / total_b:.0%} less)")
        print("cost at plan window/pricing: " + w(f"${before:,.2f} -> ${after:,.2f}/month", "1;32"))
        if risky:
            n = sum(len(v) for v in risky.values())
            print(w(f"DANGER: {n} warn/error lines would be dropped or sampled:", "1;31"))
            for rid, ex in risky.items():
                print(f"  {rid}: {len(ex)} lines, e.g. {ex[0]}")
        else:
            print(w("OK: no warn/error lines are touched by these rules.", "32"))
    if risky and args.fail_on_dropped_errors:
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="logspend",
        description="Find the log patterns that drive your observability bill and generate drop/sample "
                    "rules for the OpenTelemetry Collector, Vector and Fluent Bit.",
    )
    ap.add_argument("--version", action="version", version=f"logspend {__version__}")
    ap.add_argument("--no-color", action="store_true", help="disable ANSI colors")
    sub = ap.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("analyze", help="group log lines into patterns, cost them, suggest actions")
    a.add_argument("files", nargs="+", help="log files (.gz ok) or - for stdin")
    a.add_argument("--vendor", choices=sorted(PRICING), default="generic", help="pricing preset (default generic $0.50/GB)")
    a.add_argument("--price-gb", type=float, help="your $ per GB ingested")
    a.add_argument("--price-per-million", type=float, help="your $ per million events (indexing)")
    a.add_argument("--window", type=parse_window, help="time span the sample covers (default: from timestamps)")
    a.add_argument("--scale", type=float, default=1.0,
                   help="multiply volume, e.g. 40 if this sample is from 1 of 40 identical pods")
    a.add_argument("--plan", help="write an editable JSON plan here")
    a.add_argument("--json", action="store_true", help="print the plan as JSON")
    a.add_argument("--top", type=int, default=15, help="patterns to show (default 15)")
    a.add_argument("--message-key", help="JSON key holding the message (default: msg/message/event/...)")
    a.add_argument("--merge-threshold", type=int, default=5,
                   help="wildcard a token position with this many distinct values (0 = off, default 5)")
    a.add_argument("--min-share", type=float, default=0.05, help="byte share to suggest sampling an info pattern (0.05)")
    a.add_argument("--min-count", type=int, default=100, help="min lines to suggest sampling (100)")
    a.add_argument("--sample-keep", type=int, default=10, help="keep 1 in N for sampled patterns (10)")
    a.set_defaults(func=cmd_analyze)

    r = sub.add_parser("rules", help="render a plan as OTel Collector / Vector / Fluent Bit config")
    r.add_argument("plan")
    r.add_argument("--target", choices=TARGETS, required=True)
    r.add_argument("--field", help="field to match: " + ", ".join(f"{k} default {v}" for k, v in DEFAULT_FIELD.items()))
    r.add_argument("--inputs", default="my_source", help="Vector input/source name (default my_source)")
    r.add_argument("-o", "--output", help="write to a file instead of stdout")
    r.set_defaults(func=cmd_rules)

    s = sub.add_parser("simulate", help="apply a plan's rules to logs: savings + warn/error safety check")
    s.add_argument("plan")
    s.add_argument("files", nargs="+")
    s.add_argument("--json", action="store_true")
    s.add_argument("--fail-on-dropped-errors", action="store_true", help="exit 1 if any warn/error line is hit (CI)")
    s.set_defaults(func=cmd_simulate)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    C.on = (not args.no_color) and _color_on(sys.stdout)
    try:
        return args.func(args)
    except FileNotFoundError as e:
        print(f"logspend: {e}", file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())

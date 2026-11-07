import json
import os
import re

import pytest

from conftest import EXAMPLES
from logspend.mine import (PLACEHOLDER_RE, Pattern, mask, merge_high_cardinality, mine, monthly_cost,
                           suggest, template_regex)
from logspend.parse import parse_line, read_records


def test_mask_variables():
    m = mask("user 42 from 10.1.2.3:443 req 1b4e28ba-2fa1-11d2-883f-0016d3cca427 sha deadbeef1234 "
             "a@b.io at 2026-11-07T09:00:00Z took 12.5ms /orders/991")
    assert m == "user <NUM> from <IP> req <UUID> sha <HEX> <EMAIL> at <TS> took <NUM> /orders/<NUM>"


def test_mask_keeps_words_with_digits():
    assert mask("http2 upgrade v3 api") == "http2 upgrade v3 api"


def test_exported_regex_subset_is_portable():
    bad = re.compile(r"\(\?[=!<]|\\[1-9]|\*\+|\+\+")
    for frag in PLACEHOLDER_RE.values():
        assert not bad.search(frag)
    rx = template_regex('say "hi" it\'s #1 a\\b <NUM> (x) [y] {z} $ ^ | ? *', json_tolerant=True)
    assert not bad.search(rx)
    assert "'" not in rx and '"' not in rx and "#" not in rx


@pytest.mark.parametrize("fname", ["app.log", "nginx.log"])
def test_every_line_matches_its_own_pattern(fname):
    path = os.path.join(EXAMPLES, fname)
    recs = list(read_records([path]))
    pats, _, total = mine(iter(recs))
    assert total == len(recs)
    rxs = [re.compile(p.regex) for p in pats]
    for r in recs:
        assert any(rx.search(r.raw) for rx in rxs), r.raw


def test_json_escaped_quotes_slashes_backslashes():
    msgs = ['he said "hi" to /api/x', 'path C:\\temp\\x', "it's fine"]
    lines = [json.dumps({"msg": m}) for m in msgs]
    lines.append('{"msg":"escaped \\/api\\/y slash"}')  # PHP-style escaped slashes
    for line in lines:
        r = parse_line(line)
        rx = re.compile(template_regex(mask(r.message), json_tolerant=True))
        assert rx.search(line), (line, rx.pattern)


def test_merge_high_cardinality_names_but_not_paths_or_errors():
    pats = {}
    for name in ["alice", "bob", "carol", "dave", "erin"]:
        pats[f"user {name} logged in"] = Pattern(f"user {name} logged in", count=1)
    for path in ["/a", "/b", "/c", "/d", "/healthz"]:
        pats[f"GET {path} 200"] = Pattern(f"GET {path} 200", count=1)
    for word in ["timeout", "refused", "denied", "invalid", "panic"]:
        pats[f"db {word} now"] = Pattern(f"db {word} now", count=1)
    out = merge_high_cardinality(pats, 5)
    assert "user <*> logged in" in out and out["user <*> logged in"].count == 5
    assert "GET /healthz 200" in out and "GET <*> 200" not in out
    assert "db timeout now" in out


def test_merge_off():
    pats = {f"user {n} x": Pattern(f"user {n} x") for n in "abcdefg"}
    assert len(merge_high_cardinality(dict(pats), 0)) == 7


def _pat(template, level="INFO", count=200, bytes_=1000, message=None):
    p = Pattern(template)
    for _ in range(count):
        p.add(parse_line(json.dumps({"level": level, "msg": message or template})))
    p.bytes = bytes_
    return p


def test_suggest_rules():
    pats = [
        _pat("GET /healthz <NUM>", bytes_=100),
        _pat("cache hit <NUM>", level="debug", bytes_=100),
        _pat("big info", bytes_=10000),
        _pat("payment failed <NUM>", bytes_=10000),
        _pat("slow query", level="warn", bytes_=10000),
        _pat("small info", bytes_=10, count=5),
        _pat("GET /healthz err", level="error", bytes_=100),
    ]
    suggest(pats, min_share=0.05, min_count=100, sample_keep=20)
    acts = {p.template: (p.action, p.sample_keep) for p in pats}
    assert acts["GET /healthz <NUM>"][0] == "drop"
    assert acts["cache hit <NUM>"][0] == "drop"
    assert acts["big info"] == ("sample", 20)
    assert acts["payment failed <NUM>"][0] == "keep"
    assert acts["slow query"][0] == "keep"
    assert acts["small info"][0] == "keep"
    assert acts["GET /healthz err"][0] == "keep"


def test_mixed_level_debug_pattern_with_one_warn_is_kept():
    p = _pat("thing <NUM>", level="debug")
    p.add(parse_line(json.dumps({"level": "warn", "msg": "thing 1"})))
    suggest([p])
    assert p.action == "keep"


def test_monthly_cost():
    # 1 GB over one day -> 30 GB/month at $0.5 = $15, plus 1M events/day * 30 * $1.7
    assert monthly_cost(1e9, 1e6, 86400, 0.5, 1.7) == pytest.approx(15 + 51)


def test_window_from_timestamps():
    _, window, _ = mine(read_records([os.path.join(EXAMPLES, "app.log")]))
    assert 3500 < window <= 3600

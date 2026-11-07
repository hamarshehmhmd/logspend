import gzip
import json

from logspend.parse import parse_line, parse_time, read_records


def test_json_structlog_style():
    r = parse_line('{"time":"2026-11-07T09:00:00.500Z","level":"debug","service":"api","msg":"cache hit key=u:1"}')
    assert (r.message, r.level, r.service, r.is_json) == ("cache hit key=u:1", "DEBUG", "api", True)
    assert r.ts == parse_time("2026-11-07T09:00:00.5Z")
    assert r.size == len(r.raw) + 1


def test_pino_numeric_level_and_epoch_ms():
    r = parse_line(json.dumps({"level": 50, "time": 1762506000000, "msg": "boom"}))
    assert r.level == "ERROR"
    assert abs(r.ts - 1762506000.0) < 1e-6


def test_nested_service_and_message_key():
    r = parse_line(json.dumps({"service": {"name": "billing"}, "event": "paid", "severity": "WARNING"}))
    assert (r.service, r.message, r.level) == ("billing", "paid", "WARN")
    r2 = parse_line(json.dumps({"text": "a", "custom": "b"}), message_key="custom")
    assert r2.message == "b"


def test_json_without_message_groups_by_shape():
    r = parse_line('{"b":1,"a":2}')
    assert r.message == "{a,b}"


def test_docker_wrapper_with_inner_json():
    inner = json.dumps({"level": "info", "msg": "GET /healthz 200"})
    line = json.dumps({"log": inner + "\n", "stream": "stdout", "time": "2026-11-07T09:00:01Z"})
    r = parse_line(line)
    assert (r.message, r.level) == ("GET /healthz 200", "INFO")
    assert r.raw == line and r.ts == parse_time("2026-11-07T09:00:01Z")


def test_cri_prefix_plain():
    r = parse_line("2026-11-07T09:00:02.1+02:00 stdout F WARN disk almost full")
    assert r.level == "WARN"
    assert r.ts == parse_time("2026-11-07T07:00:02.1Z")


def test_logfmt_msg_and_level():
    r = parse_line('time="2026-11-07T09:00:00Z" level=error msg="dial tcp: refused" peer=db')
    assert (r.level, r.message) == ("ERROR", "dial tcp: refused")


def test_plain_text_prefix_stripped():
    r = parse_line("2026-11-07 09:00:00,123 INFO [main] started in 12ms")
    assert r.level == "INFO"
    assert r.message.startswith("INFO [main]")
    assert r.ts is not None


def test_unknown_level():
    assert parse_line("just some text").level == "-"


def test_read_records_gz_and_blank_lines(tmp_path):
    p = tmp_path / "a.log.gz"
    with gzip.open(p, "wt") as fh:
        fh.write("one\n\n two \n")
    assert [r.message for r in read_records([str(p)])] == ["one", " two "]

import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys

import pytest

from conftest import EXAMPLES, ROOT
from logspend.cli import main, parse_window

APP = os.path.join(EXAMPLES, "app.log")


def run(*args, stdin=None):
    env = dict(os.environ, PYTHONPATH=os.path.join(ROOT, "src"), NO_COLOR="1")
    return subprocess.run([sys.executable, "-m", "logspend", *args], capture_output=True, text=True,
                          input=stdin, env=env)


@pytest.fixture
def plan(tmp_path):
    path = tmp_path / "plan.json"
    assert main(["analyze", APP, "--vendor", "datadog", "--plan", str(path), "--json"]) == 0
    return path


def test_parse_window():
    assert parse_window("90s") == 90 and parse_window("15m") == 900 and parse_window("2h") == 7200
    assert parse_window("1d") == 86400 and parse_window("30") == 30


def test_analyze_text_output():
    p = run("analyze", APP, "--vendor", "datadog", "--scale", "40")
    assert p.returncode == 0, p.stderr
    out = p.stdout
    assert "23,920 lines" in out and "from timestamps" in out and "scaled x40" in out
    assert re.search(r"p1 .*drop .*GET /healthz", out)
    assert "Warn/error lines" in out and "-68%" in out


def test_plan_contents(plan):
    d = json.loads(plan.read_text())
    assert d["lines"] == 23920 and d["pricing"]["name"] == "datadog"
    costs = [p["monthly_cost"] for p in d["patterns"]]
    assert costs == sorted(costs, reverse=True)
    by_t = {p["template"]: p for p in d["patterns"]}
    assert by_t["GET /healthz <NUM> <NUM>"]["action"] == "drop"
    for p in d["patterns"]:
        if p["max_level"] in ("WARN", "ERROR", "FATAL") or "failed" in p["template"]:
            assert p["action"] == "keep"
    assert d["monthly_cost_after"] < d["monthly_cost_now"]


def test_assumed_window_warning_and_custom_price():
    p = run("analyze", os.path.join(EXAMPLES, "nginx.log"), "--price-gb", "2.5")
    assert p.returncode == 0
    assert "ASSUMED 24 h" in p.stdout and "custom: $2.5/GB" in p.stdout


def test_stdin_and_gz(tmp_path):
    data = open(os.path.join(EXAMPLES, "nginx.log")).read()
    p = run("analyze", "-", "--window", "1h", stdin=data)
    assert p.returncode == 0 and "4 lines" in p.stdout
    gz = tmp_path / "n.log.gz"
    with gzip.open(gz, "wt") as fh:
        fh.write(data)
    assert "4 lines" in run("analyze", str(gz), "--window", "1h").stdout


def test_empty_input_and_missing_file(tmp_path):
    e = tmp_path / "empty.log"
    e.write_text("\n")
    assert run("analyze", str(e)).returncode == 2
    p = run("analyze", str(tmp_path / "nope.log"))
    assert p.returncode == 2 and "No such file" in p.stderr


def test_simulate_ok(plan):
    p = run("simulate", str(plan), APP, "--json", "--fail-on-dropped-errors")
    assert p.returncode == 0, p.stdout
    d = json.loads(p.stdout)
    assert d["warn_error_lines_hit"] == {} and d["unused_rules"] == []
    assert d["bytes_after"] < d["bytes_before"] * 0.4


def test_simulate_flags_dangerous_rule(plan):
    d = json.loads(plan.read_text())
    for p in d["patterns"]:
        if p["template"].startswith("slow query"):
            p["action"] = "drop"
    plan.write_text(json.dumps(d))
    p = run("simulate", str(plan), APP, "--fail-on-dropped-errors")
    assert p.returncode == 1
    assert "DANGER" in p.stdout
    assert run("simulate", str(plan), APP).returncode == 0


def test_rules_targets(plan):
    for target, needle in [("otel", "IsMatch(body,"), ("vector", "match(logspend_m, r'"), ("fluentbit", "Exclude log ")]:
        p = run("rules", str(plan), "--target", target)
        assert p.returncode == 0 and needle in p.stdout
    assert "NOT APPLIED" in run("rules", str(plan), "--target", "otel").stdout
    assert "random_int(0, 10)" in run("rules", str(plan), "--target", "vector").stdout
    assert "IsMatch(attributes[" in run("rules", str(plan), "--target", "otel", "--field", 'attributes["msg"]').stdout


def test_rules_rejects_bad_plan(tmp_path, plan):
    d = json.loads(plan.read_text())
    d["patterns"][0]["action"] = "nuke"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(d))
    p = run("rules", str(bad), "--target", "otel")
    assert p.returncode != 0 and "action must be" in p.stderr
    (tmp_path / "x.json").write_text("{}")
    assert run("rules", str(tmp_path / "x.json"), "--target", "otel").returncode != 0


def test_version():
    p = run("--version")
    assert p.stdout.strip() == "logspend 0.1.0"

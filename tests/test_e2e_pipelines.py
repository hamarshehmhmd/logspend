"""Run the generated config in the real OpenTelemetry Collector, Vector and Fluent Bit.

Each test is skipped when its binary is not installed. Point LOGSPEND_OTELCOL / LOGSPEND_VECTOR /
LOGSPEND_FLUENTBIT at a binary, or put otelcol-contrib / vector / fluent-bit on PATH. CI runs all three.
"""
import json
import os
import re
import shutil
import subprocess
import time

import pytest

from conftest import EXAMPLES
from logspend.cli import main


def _bin(env, *names):
    p = os.environ.get(env)
    if p and os.path.exists(p):
        return p
    for n in names:
        if shutil.which(n):
            return shutil.which(n)
    return None


OTELCOL = _bin("LOGSPEND_OTELCOL", "otelcol-contrib")
VECTOR = _bin("LOGSPEND_VECTOR", "vector")
FLUENTBIT = _bin("LOGSPEND_FLUENTBIT", "fluent-bit")


@pytest.fixture
def setup(tmp_path):
    src = tmp_path / "in.log"
    with open(os.path.join(EXAMPLES, "app.log")) as fh:
        src.write_text("".join(fh.readlines()[:3000]))
    plan = tmp_path / "plan.json"
    assert main(["analyze", str(src), "--plan", str(plan), "--json"]) == 0
    d = json.loads(plan.read_text())
    for p in d["patterns"]:
        if p["action"] == "sample":  # make the outcome deterministic
            p["action"] = "drop"
    plan.write_text(json.dumps(d))
    rx = [re.compile(p["regex"]) for p in d["patterns"] if p["action"] == "drop"]
    lines = src.read_text().splitlines()
    expected = [l for l in lines if not any(r.search(l) for r in rx)]
    assert 0 < len(expected) < len(lines)
    return tmp_path, src, plan, expected


@pytest.mark.skipif(not OTELCOL, reason="otelcol-contrib not installed")
def test_otel_collector_drops_exactly_what_simulate_predicts(setup):
    tmp, src, plan, expected = setup
    assert main(["rules", str(plan), "--target", "otel", "-o", str(tmp / "rules.yaml")]) == 0
    out = tmp / "out.jsonl"
    (tmp / "base.yaml").write_text(f"""
receivers:
  filelog:
    include: [{src}]
    start_at: beginning
exporters:
  file:
    path: {out}
service:
  telemetry:
    metrics:
      level: none
  pipelines:
    logs:
      receivers: [filelog]
      processors: [filter/logspend]
      exporters: [file]
""")
    cfg = ["--config", str(tmp / "base.yaml"), "--config", str(tmp / "rules.yaml")]
    v = subprocess.run([OTELCOL, "validate", *cfg], capture_output=True, text=True)
    assert v.returncode == 0, v.stderr
    proc = subprocess.Popen([OTELCOL, *cfg], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        bodies = []
        deadline = time.time() + 60
        while time.time() < deadline:
            time.sleep(1)
            if not out.exists():
                continue
            bodies = []
            for line in out.read_text().splitlines():
                for rl in json.loads(line)["resourceLogs"]:
                    for sl in rl["scopeLogs"]:
                        bodies += [r["body"]["stringValue"] for r in sl["logRecords"]]
            if len(bodies) >= len(expected):
                time.sleep(2)
                break
    finally:
        proc.terminate()
        proc.wait(10)
    bodies = []
    for line in out.read_text().splitlines():
        for rl in json.loads(line)["resourceLogs"]:
            for sl in rl["scopeLogs"]:
                bodies += [r["body"]["stringValue"] for r in sl["logRecords"]]
    assert sorted(bodies) == sorted(expected)


@pytest.mark.skipif(not VECTOR, reason="vector not installed")
def test_vector_drops_exactly_what_simulate_predicts(setup):
    tmp, src, plan, expected = setup
    assert main(["rules", str(plan), "--target", "vector", "--inputs", "in", "-o", str(tmp / "rules.toml")]) == 0
    (tmp / "base.toml").write_text('[sources.in]\ntype = "stdin"\n\n[sinks.out]\ntype = "console"\n'
                                   'inputs = ["logspend"]\nencoding.codec = "text"\n')
    cfg = [str(tmp / "base.toml"), str(tmp / "rules.toml")]
    v = subprocess.run([VECTOR, "validate", "--no-environment", *cfg], capture_output=True, text=True)
    assert v.returncode == 0, v.stdout + v.stderr
    p = subprocess.run([VECTOR, "--quiet", "-c", cfg[0], "-c", cfg[1]], input=src.read_text(),
                       capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stderr
    assert sorted(p.stdout.splitlines()) == sorted(expected)


@pytest.mark.skipif(not VECTOR, reason="vector not installed")
def test_vector_samples_roughly_one_in_ten(tmp_path):
    src = tmp_path / "in.log"
    src.write_text("".join('{"level":"info","msg":"stripe webhook received","n":%d}\n' % i for i in range(4000)))
    plan = tmp_path / "plan.json"
    assert main(["analyze", str(src), "--window", "1h", "--plan", str(plan), "--json"]) == 0
    d = json.loads(plan.read_text())
    assert [p["action"] for p in d["patterns"]] == ["sample"]
    main(["rules", str(plan), "--target", "vector", "--inputs", "in", "-o", str(tmp_path / "r.toml")])
    (tmp_path / "b.toml").write_text('[sources.in]\ntype = "stdin"\n\n[sinks.out]\ntype = "console"\n'
                                     'inputs = ["logspend"]\nencoding.codec = "text"\n')
    p = subprocess.run([VECTOR, "--quiet", "-c", str(tmp_path / "b.toml"), "-c", str(tmp_path / "r.toml")],
                       input=src.read_text(), capture_output=True, text=True, timeout=120)
    kept = len(p.stdout.splitlines())
    assert 250 < kept < 600, kept


@pytest.mark.skipif(not FLUENTBIT, reason="fluent-bit not installed")
def test_fluentbit_drops_exactly_what_simulate_predicts(setup):
    tmp, src, plan, expected = setup
    assert main(["rules", str(plan), "--target", "fluentbit", "-o", str(tmp / "rules.conf")]) == 0
    out = tmp / "out"
    out.mkdir()
    (tmp / "fb.conf").write_text(f"""[SERVICE]
    Flush        1
    Log_Level    error

[INPUT]
    Name             tail
    Path             {src}
    Read_from_Head   On
    Exit_On_Eof      On
    Tag              app

@INCLUDE {tmp / 'rules.conf'}

[OUTPUT]
    Name    file
    Match   *
    Path    {out}
    File    kept.txt
    Format  template
    Template {{log}}
""")
    p = subprocess.run([FLUENTBIT, "-c", str(tmp / "fb.conf")], capture_output=True, text=True, timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr
    kept = (out / "kept.txt").read_text().splitlines()
    assert sorted(kept) == sorted(expected)

"""Generate a realistic, deterministic sample of one hour of JSON logs from a small web app.

python examples/gen.py > examples/app.log
"""
import json
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

rnd = random.Random(7)
start = datetime(2026, 11, 7, 9, 0, 0, tzinfo=timezone.utc)
names = ["alice", "bob", "carol", "dave", "erin", "frank", "grace", "heidi", "ivan", "judy"]
paths = ["/api/orders/{n}", "/api/users/{n}", "/api/cart", "/api/search", "/api/products/{n}"]


def emit(t, level, msg, service="api", **extra):
    rec = {"time": t.isoformat(timespec="milliseconds").replace("+00:00", "Z"), "level": level,
           "service": service, "msg": msg}
    rec.update(extra)
    sys.stdout.write(json.dumps(rec, separators=(",", ":")) + "\n")


t = start
end = start + timedelta(hours=1)
while t < end:
    t += timedelta(milliseconds=rnd.randint(60, 240))
    r = rnd.random()
    if r < 0.30:
        emit(t, "info", f"GET /healthz 200 {rnd.uniform(0.1, 0.9):.1f}ms", ua="kube-probe/1.31",
             trace_id=uuid.UUID(int=rnd.getrandbits(128)).hex)
    elif r < 0.48:
        emit(t, "debug", f"SELECT id, status, total FROM orders WHERE user_id = {rnd.randint(1, 9999)} LIMIT 50 ({rnd.uniform(0.5, 9):.1f}ms)",
             service="api", component="db")
    elif r < 0.58:
        emit(t, "debug", f"cache hit key=user:{rnd.randint(1, 9999)}", service="api")
    elif r < 0.80:
        p = rnd.choice(paths).format(n=rnd.randint(1, 99999))
        emit(t, "info", f"{rnd.choice(['GET', 'GET', 'POST'])} {p} 200 {rnd.randint(3, 180)}ms",
             request_id=str(uuid.UUID(int=rnd.getrandbits(128))), user_agent="Mozilla/5.0 (X11; Linux x86_64)")
    elif r < 0.86:
        emit(t, "info", "stripe webhook received", service="billing",
             payload={"id": "evt_" + uuid.UUID(int=rnd.getrandbits(128)).hex[:24], "type": "invoice.paid",
                      "data": {"lines": [{"amount": rnd.randint(100, 9999), "desc": "Pro plan " + "x" * 40} for _ in range(12)]}})
    elif r < 0.90:
        emit(t, "info", f"user {rnd.choice(names)} logged in", service="auth")
    elif r < 0.94:
        emit(t, "info", f"worker heartbeat ok queue=emails depth={rnd.randint(0, 40)}", service="worker")
    elif r < 0.97:
        emit(t, "info", f"processing job {uuid.UUID(int=rnd.getrandbits(128))} attempt={rnd.randint(1, 3)}", service="worker")
    elif r < 0.99:
        emit(t, "warn", f"slow query took {rnd.randint(800, 3000)}ms", service="api", component="db")
    else:
        emit(t, "error", f"payment failed for order {rnd.randint(1000, 99999)}: card_declined", service="billing")

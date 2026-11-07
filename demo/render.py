"""Render the logspend narrated demo: real command output + edge-tts narration.

Usage: python demo/render.py [--gif]
(needs ffmpeg, edge-tts, Pillow, network; logspend installed in /tmp/lsv; otelcol-contrib in /tmp/bins)
"""
import asyncio
import os
import re
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(HERE, "build")
OUT = os.path.join(ROOT, "demo.mp4")
SDKBIN = "/tmp/lsv/bin:/tmp/bins"
SANDBOX = "/tmp/lsdemo"
W, H, FPS = 1280, 720, 24
VOICE = "en-US-AndrewNeural"
MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SANS_B = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
BG, FG = (24, 26, 33), (220, 223, 228)
ANSI = {"31": (255, 95, 95), "32": (95, 215, 120), "33": (240, 200, 80), "2": (130, 135, 145), "36": (90, 200, 230), "1": FG}
PROMPT = "sre@laptop:~/logs $ "

APP = SANDBOX + "/logs"
SCENES = [
    {"kind": "title", "title": "logspend", "sub": "find the log patterns driving your bill\nand get drop rules you can trust",
     "say": "This is log spend, a small open source command line tool that finds the log lines that drive your "
            "observability bill, and writes the rules to stop paying for them."},
    {"kind": "bullets", "title": "The problem",
     "bullets": ["Datadog bills per GB and per million indexed events",
                 "Most of it: health checks, debug SQL, cache hits, heartbeats",
                 "Teams find out at renewal, then cut visibility blindly",
                 "Hand-written pipeline regexes can drop the errors you need",
                 "Cost tools tell you the total, not which line to remove"],
     "say": "Log bills grow faster than the product. Datadog charges per gigabyte and per million indexed events, and most "
            "of that money goes on lines nobody reads: kube probe health checks, debug SQL left on in production, cache hits, "
            "heartbeats. Teams usually find out at renewal, then hand-write regexes for their pipeline and hope they "
            "didn't just drop the errors they'll need at three A M."},
    {"kind": "term", "cwd": APP,
     "cmds": ["ls -lh api.log", "head -n 3 api.log | cut -c1-130"],
     "say": "Here's one hour of real looking J S O N logs from a small web app. About twenty four thousand lines, five megabytes. "
            "In production this would be one of forty pods."},
    {"kind": "term", "cwd": APP,
     "cmds": ["logspend analyze api.log --vendor datadog --scale 40 --top 8 --plan plan.json"],
     "say": "Log spend analyze groups every line into a template, masking numbers, I Ds and durations, and prices each "
            "pattern at Datadog's list prices, scaled to forty pods. The health check alone costs about three hundred and "
            "fifty dollars a month. Debug SQL and cache hits are next. It suggests drops for debug, probes and heartbeats, "
            "and sampling for the big webhook payloads, cutting the projected bill by about two thirds. "
            "Warnings, errors and anything that mentions a failure are never touched."},
    {"kind": "term", "cwd": APP,
     "cmds": ["logspend simulate plan.json api.log --fail-on-dropped-errors"],
     "say": "The plan is a J S O N file you can edit. Simulate replays it against the logs: which rule matched how many lines, "
            "the new size and cost, and a safety check that no warning or error line would be lost."},
    {"kind": "term", "cwd": APP,
     "cmds": ["jq '(.patterns[]|select(.template|startswith(\"slow query\"))|.action)=\"drop\"' plan.json > risky.json",
              "logspend simulate risky.json api.log --fail-on-dropped-errors; echo \"exit=$?\""],
     "say": "Watch what happens if someone gets greedy and drops the slow query warnings. Simulate flags every warning it "
            "would lose and exits non zero, so you can put it in C I next to your pipeline config."},
    {"kind": "term", "cwd": APP,
     "cmds": ["logspend rules plan.json --target otel > otel-rules.yaml && sed -n '5,12p' otel-rules.yaml",
              "otelcol-contrib validate --config collector.yaml --config otel-rules.yaml && echo 'collector: config valid'"],
     "say": "Rules turns the plan into config. Here's the OpenTelemetry Collector filter processor, with the monthly cost "
            "of each pattern as a comment, and the real collector binary accepting it."},
    {"kind": "term", "cwd": APP,
     "cmds": ["logspend rules plan.json --target vector --inputs kubernetes_logs | sed -n '3,12p' | cut -c1-120",
              "logspend rules plan.json --target fluentbit | sed -n '3,7p' | cut -c1-120"],
     "say": "It also writes a Vector remap transform, which can sample as well as drop, and a Fluent Bit grep filter. "
            "The test suite pushes logs through the real collector, Vector and Fluent Bit, and checks that exactly the "
            "lines simulate predicted survive."},
    {"kind": "bullets", "title": "What you get",
     "bullets": ["Cost per log pattern at your vendor's prices",
                 "Suggestions that never touch warn/error lines",
                 "An editable plan + simulate with a CI safety gate",
                 "OTel Collector, Vector and Fluent Bit rules",
                 "Zero dependencies; your logs never leave the machine"],
     "say": "So: cost per pattern at your vendor's prices, safe suggestions, an editable plan with a C I safety gate, and "
            "rules for the three pipelines most teams already run. No agent, no SaaS, and your logs stay on your machine."},
    {"kind": "bullets", "title": "Try it",
     "bullets": ["curl -fsSL https://raw.githubusercontent.com/",
                 "   hamarshehmhmd/logspend/main/install.sh | sh",
                 "logspend analyze app.log --vendor datadog --plan plan.json",
                 "github.com/hamarshehmhmd/logspend",
                 "GNU AGPL-3.0 (commercial licenses available)"],
     "say": "Install it with the one line installer. It's licensed under the GNU A G P L version three or later, "
            "with commercial licenses available from the author. It's on GitHub at hamarsheh m h m d slash log spend."},
]


async def tts(text, path):
    import edge_tts
    for attempt in range(8):
        try:
            await edge_tts.Communicate(text, VOICE, rate="+4%").save(path)
            return
        except Exception:
            if os.path.exists(path):
                os.remove(path)
            if attempt == 7:
                raise
            await asyncio.sleep(5)


def duration(path):
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        text=True,
    )
    return float(out.strip())


EXPORTS = {}


def run_cmd(cmd, cwd):
    if cmd.startswith("export ") and "=" in cmd:
        k, v = cmd[7:].split("=", 1)
        EXPORTS[k] = v
        return ""
    env = dict(os.environ, FORCE_COLOR="1", **EXPORTS)
    env.pop("NO_COLOR", None)
    env["PATH"] = SDKBIN + os.pathsep + env["PATH"]
    env["COLUMNS"] = "120"
    p = subprocess.run(
        ["bash", "-c", "shopt -s nullglob; " + cmd],
        cwd=cwd, capture_output=True, text=True, env=env,
    )
    return (p.stdout + p.stderr).rstrip("\n")


def parse_ansi(line):
    spans, color, bold = [], FG, False
    for part in re.split(r"(\x1b\[[0-9;]*m)", line):
        m = re.match(r"\x1b\[([0-9;]*)m", part)
        if m:
            for code in (m.group(1) or "0").split(";"):
                if code == "0":
                    color, bold = FG, False
                elif code == "1":
                    bold = True
                elif code in ANSI:
                    color = ANSI[code]
        elif part:
            spans.append((part, color, bold))
    return spans


class Term:
    def __init__(self):
        self.font = ImageFont.truetype(MONO, 14)
        self.bold = ImageFont.truetype(MONO_B, 14)
        self.cw = self.font.getlength("M")
        self.lh = 20
        self.cols = int((W - 80) / self.cw)

    def wrap(self, spans):
        lines, cur, n = [], [], 0
        for text, c, b in spans:
            while text:
                room = self.cols - n
                if len(text) <= room:
                    cur.append((text, c, b)); n += len(text); text = ""
                else:
                    cut = text.rfind(" ", 0, room + 1)
                    cut = cut if cut > 0 else room
                    cur.append((text[:cut], c, b)); lines.append(cur); cur, n = [], 0
                    text = "    " + text[cut:].lstrip(" ")
        lines.append(cur)
        return lines

    def frame(self, lines, cursor=False):
        img = Image.new("RGB", (W, H), BG)
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([20, 14, W - 20, H - 14], 12, fill=(30, 33, 41), outline=(60, 64, 76))
        for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
            d.ellipse([40 + i * 24, 30, 54 + i * 24, 44], fill=c)
        d.text((W // 2, 37), "~/logs — bash", font=ImageFont.truetype(SANS, 15), fill=(150, 155, 165), anchor="mm")
        visible = lines[-30:]
        y = 64
        for ln in visible:
            x = 40
            for text, c, b in ln:
                d.text((x, y), text, font=self.bold if b else self.font, fill=c)
                x += self.cw * len(text)
            y += self.lh
        if cursor:
            last = visible[-1] if visible else []
            x = 40 + self.cw * sum(len(t) for t, _, _ in last)
            d.rectangle([x, y - self.lh + 3, x + self.cw - 2, y - 4], fill=FG)
        return img


def card(scene):
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    if scene["kind"] == "title":
        d.text((W // 2, 280), scene["title"], font=ImageFont.truetype(MONO_B, 84), fill=(95, 215, 120), anchor="mm")
        d.multiline_text((W // 2, 420), scene["sub"], font=ImageFont.truetype(SANS, 34), fill=FG, anchor="mm",
                         align="center", spacing=12)
        d.text((W // 2, 620), "github.com/hamarshehmhmd/logspend  ·  OTel · Vector · Fluent Bit  ·  GNU AGPL-3.0",
               font=ImageFont.truetype(SANS, 22), fill=(150, 155, 165), anchor="mm")
    else:
        d.text((90, 90), scene["title"], font=ImageFont.truetype(SANS_B, 52), fill=(95, 215, 120))
        f = ImageFont.truetype(SANS, 29)
        y = 200
        for b in scene["bullets"]:
            if b and not b.startswith("   "):
                d.text((90, y), "•", font=f, fill=(240, 200, 80))
            d.text((130, y), b.strip(), font=f, fill=FG)
            y += 58 if b.startswith("   ") or not b else 62
    return img


class Writer:
    def __init__(self, path, audio):
        self.p = subprocess.Popen(
            ["ffmpeg", "-y", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "rgb24",
             "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-", "-i", audio,
             "-af", "apad", "-c:v", "libx264", "-preset", "medium", "-crf", "23",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
             "-shortest", path],
            stdin=subprocess.PIPE,
        )
        self.n = 0

    def put(self, img, count=1):
        data = img.tobytes()
        for _ in range(count):
            self.p.stdin.write(data)
        self.n += count

    def close(self):
        self.p.stdin.close()
        self.p.wait()
        if self.p.returncode:
            sys.exit("ffmpeg failed")


def render_scene(i, scene, workdir):
    workdir = scene.get("cwd", APP)
    audio = os.path.join(BUILD, f"s{i}.mp3")
    if not os.path.exists(audio):
        asyncio.run(tts(scene["say"], audio))
    total = int((duration(audio) + 0.7) * FPS)
    path = os.path.join(BUILD, f"s{i}.mp4")
    w = Writer(path, audio)
    if scene["kind"] != "term":
        w.put(card(scene), total)
        w.close()
        return path
    t = Term()
    shown = scene.get("show", scene["cmds"])
    outputs = [run_cmd(c, workdir) for c in scene["cmds"]]
    # Truncate very long check output so it fits the terminal window
    capped = []
    for out in outputs:
        lines = out.splitlines()
        if len(lines) > 26:
            lines = lines[:24] + ["…", lines[-1]]
        capped.append("\n".join(lines))
    outputs = capped
    steps = list(zip(shown, outputs))
    typing = sum(len(s) for s, _ in steps) / 32 * FPS
    pause = max(6, int((total - typing) / max(1, len(steps))) - 4)
    lines = []
    for cmd, out in steps:
        prompt = [(PROMPT, (95, 215, 120), True)]
        for k in range(0, len(cmd) + 1, 2):
            frame = t.frame(lines + t.wrap(prompt + [(cmd[:k], FG, False)]), cursor=True)
            w.put(frame)
        lines += t.wrap(prompt + [(cmd, FG, False)])
        for ol in out.splitlines():
            lines += t.wrap(parse_ansi(ol))
        lines.append([])
        hold = min(pause, max(0, total - w.n))
        w.put(t.frame(lines), hold)
    final = t.frame(lines + t.wrap([(PROMPT, (95, 215, 120), True)]), cursor=True)
    if w.n < total:
        w.put(final, total - w.n)
    w.close()
    return path


COLLECTOR = """receivers:
  file_log:
    include: [/var/log/pods/*/*/*.log]
exporters:
  debug: {}
service:
  pipelines:
    logs:
      receivers: [file_log]
      processors: [filter/logspend]
      exporters: [debug]
"""


def prepare():
    os.makedirs(BUILD, exist_ok=True)
    if os.path.exists(SANDBOX):
        shutil.rmtree(SANDBOX)
    os.makedirs(APP)
    shutil.copy(os.path.join(ROOT, "examples", "app.log"), os.path.join(APP, "api.log"))
    with open(os.path.join(APP, "collector.yaml"), "w") as fh:
        fh.write(COLLECTOR)


def gif():
    """Short silent GIF for the README: analyze, simulate, rules."""
    prepare()
    t = Term()
    cmds = ["logspend analyze api.log --vendor datadog --scale 40 --top 6 --plan plan.json",
            "logspend simulate plan.json api.log --fail-on-dropped-errors",
            "logspend rules plan.json --target otel | sed -n '9,11p' | cut -c1-110"]
    frames_dir = os.path.join(BUILD, "gif")
    shutil.rmtree(frames_dir, ignore_errors=True)
    os.makedirs(frames_dir)
    imgs, lines = [], []
    prompt = [(PROMPT, (95, 215, 120), True)]
    for cmd in cmds:
        out = run_cmd(cmd, APP)
        for k in range(0, len(cmd) + 1, 3):
            imgs.append((t.frame(lines + t.wrap(prompt + [(cmd[:k], FG, False)]), cursor=True), 1))
        lines += t.wrap(prompt + [(cmd, FG, False)])
        imgs.append((t.frame(lines), 4))
        for ol in out.splitlines():
            lines += t.wrap(parse_ansi(ol))
        lines.append([])
        imgs.append((t.frame(lines), 40))
    imgs.append((t.frame(lines), 40))
    n = 0
    for img, count in imgs:
        for _ in range(count):
            img.resize((960, 540)).save(os.path.join(frames_dir, f"f{n:05d}.png"))
            n += 1
    dst = os.path.join(ROOT, "docs", "demo.gif")
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-framerate", "12", "-i", os.path.join(frames_dir, "f%05d.png"),
                    "-vf", "split[a][b];[a]palettegen=max_colors=64[p];[b][p]paletteuse=dither=none", dst], check=True)
    print(dst, os.path.getsize(dst) // 1024, "KB")


def main():
    if "--gif" in sys.argv:
        return gif()
    prepare()
    parts = [render_scene(i, s, SANDBOX) for i, s in enumerate(SCENES)]
    with open(os.path.join(BUILD, "list.txt"), "w") as fh:
        fh.writelines(f"file '{p}'\n" for p in parts)
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i",
         os.path.join(BUILD, "list.txt"), "-c:v", "libx264", "-crf", "23", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", OUT],
        check=True,
    )
    with open(os.path.join(BUILD, "narration.txt"), "w") as fh:
        fh.write("\n\n".join(s["say"] for s in SCENES) + "\n")
    print(OUT, round(duration(OUT), 1), "s")


if __name__ == "__main__":
    main()

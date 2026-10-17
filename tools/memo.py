#!/usr/bin/env python3
"""Morning Memo builder.

  python tools/memo.py all        # samples (only missing ones) + episodes
  python tools/memo.py episodes   # render any scripts/*.md without an MP3, rebuild feed
  python tools/memo.py samples    # render the voice sampler into site/samples
  python tools/memo.py feed       # rebuild feed + index only

Needs OPENROUTER_API_KEY in the environment, plus ffmpeg/ffprobe on PATH.
The published site lives in ./site and is deployed to GitHub Pages by the workflow.
"""
import datetime as dt
import email.utils
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CFG = json.loads((ROOT / "config.json").read_text())
SCRIPTS = ROOT / "scripts"
SITE = ROOT / "site"
PUB = SITE / CFG["feed_path"]          # unguessable folder holding feed + audio
EPISODES = PUB / "episodes"
SAMPLES = PUB / "samples"
API = "https://openrouter.ai/api/v1/audio/speech"


def annotate(msg: str):
    """Surface a message as a GitHub Actions annotation (readable without raw logs)."""
    print(f"::warning::{msg}" if os.environ.get("GITHUB_ACTIONS") else msg, flush=True)


# ---------- text ----------

def speakable(md: str) -> str:
    """Strip markdown so the voice doesn't read symbols aloud."""
    out = []
    for line in md.splitlines():
        line = re.sub(r"^\s*#+\s*", "", line)          # headings become plain lines
        line = re.sub(r"^\s*[-*]\s+", "", line)        # bullets
        line = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", line)  # links
        line = re.sub(r"[*_`]", "", line)
        line = line.replace("—", ", ").replace("–", "-")
        out.append(line.strip())
    text = "\n".join(out)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def chunks(text: str, limit: int):
    """Split on paragraphs, then sentences, never mid-sentence."""
    parts, cur = [], ""
    for para in text.split("\n\n"):
        pieces = [para] if len(para) <= limit else re.split(r"(?<=[.!?])\s+", para)
        for p in pieces:
            if len(cur) + len(p) + 2 > limit and cur:
                parts.append(cur.strip())
                cur = ""
            cur += p + "\n\n"
    if cur.strip():
        parts.append(cur.strip())
    return parts


# ---------- audio ----------

def tts(text: str, model: str, voice: str) -> bytes:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        sys.exit("OPENROUTER_API_KEY is not set")
    body = json.dumps({"model": model, "input": text, "voice": voice,
                       "response_format": "mp3"}).encode()
    for attempt in range(4):
        req = urllib.request.Request(API, data=body, headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "HTTP-Referer": CFG["site_url"],
            "X-Title": CFG["title"],
        })
        try:
            with urllib.request.urlopen(req, timeout=600) as r:
                data = r.read()
            if len(data) < 1000:
                raise RuntimeError(f"suspiciously small audio ({len(data)} bytes)")
            return data
        except urllib.error.HTTPError as e:
            msg = e.read().decode(errors="replace")[:400]
            if e.code in (429, 500, 502, 503, 504) and attempt < 3:
                time.sleep(5 * 2 ** attempt)
                continue
            raise RuntimeError(f"TTS {model} failed: HTTP {e.code} {msg}")
        except (urllib.error.URLError, TimeoutError, RuntimeError):
            if attempt < 3:
                time.sleep(5 * 2 ** attempt)
                continue
            raise


def render(text: str, model: str, voice: str, out: Path, limit: int):
    """TTS each chunk, normalise to one format, join with short pauses, encode MP3."""
    out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        wavs = []
        for i, piece in enumerate(chunks(text, limit)):
            raw = tmp / f"raw{i}"
            raw.write_bytes(tts(piece, model, voice))
            wav = tmp / f"c{i}.wav"
            # format-agnostic: ffmpeg sniffs mp3/wav/ogg; raw PCM gets a second try
            r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw,
                                "-ar", "44100", "-ac", "1", wav], capture_output=True)
            if r.returncode != 0:
                subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "s16le",
                                "-ar", "24000", "-ac", "1", "-i", raw,
                                "-ar", "44100", "-ac", "1", wav], check=True)
            wavs.append(wav)
        gap = tmp / "gap.wav"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i",
                        "anullsrc=r=44100:cl=mono", "-t", "0.6", gap], check=True)
        lst = tmp / "list.txt"
        seq = []
        for i, w in enumerate(wavs):
            if i:
                seq.append(gap)
            seq.append(w)
        lst.write_text("".join(f"file '{p}'\n" for p in seq))
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                        "-i", lst, "-af", "loudnorm=I=-16:TP=-1.5:LRA=11",
                        "-codec:a", "libmp3lame", "-b:a", "96k", "-ar", "44100", out],
                       check=True)


def duration(p: Path) -> int:
    r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                        "-of", "default=nw=1:nk=1", p], capture_output=True, text=True)
    try:
        return int(float(r.stdout.strip()))
    except ValueError:
        return 0


# ---------- feed ----------

def episode_title(day: dt.date) -> str:
    return f"{CFG['title']} - {day.strftime('%a %b')} {day.day}"


def build_feed():
    PUB.mkdir(parents=True, exist_ok=True)
    base = f"{CFG['site_url'].rstrip('/')}/{CFG['feed_path']}"
    eps = sorted(EPISODES.glob("*.mp3"), reverse=True) if EPISODES.exists() else []
    items = []
    for mp3 in eps:
        day = dt.date.fromisoformat(mp3.stem)
        script = SCRIPTS / f"{mp3.stem}.md"
        summary = ""
        if script.exists():
            first = next((l for l in speakable(script.read_text()).splitlines() if l), "")
            summary = first[:240]
        pub = dt.datetime.combine(day, dt.time(6, 0), tzinfo=dt.timezone(dt.timedelta(hours=-4)))
        secs = duration(mp3)
        items.append(f"""    <item>
      <title>{html.escape(episode_title(day))}</title>
      <description>{html.escape(summary)}</description>
      <enclosure url="{base}/episodes/{mp3.name}" length="{mp3.stat().st_size}" type="audio/mpeg"/>
      <guid isPermaLink="false">memo-{mp3.stem}</guid>
      <pubDate>{email.utils.format_datetime(pub)}</pubDate>
      <itunes:duration>{secs // 60}:{secs % 60:02d}</itunes:duration>
      <itunes:explicit>false</itunes:explicit>
    </item>""")
    feed = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd">
  <channel>
    <title>{html.escape(CFG['title'])}</title>
    <link>{base}/</link>
    <description>{html.escape(CFG['description'])}</description>
    <language>en-us</language>
    <itunes:author>{html.escape(CFG['author'])}</itunes:author>
    <itunes:image href="{base}/cover.png"/>
    <itunes:explicit>false</itunes:explicit>
    <itunes:block>Yes</itunes:block>
    <itunes:category text="News"/>
{chr(10).join(items)}
  </channel>
</rss>
"""
    (PUB / "feed.xml").write_text(feed)
    (SITE / "index.html").write_text("<!doctype html><meta charset=utf-8><title>.</title>")
    (SITE / ".nojekyll").write_text("")
    print(f"feed: {len(items)} episodes -> {base}/feed.xml")


def prune():
    keep = CFG.get("keep_episodes", 30)
    eps = sorted(EPISODES.glob("*.mp3"), reverse=True) if EPISODES.exists() else []
    for old in eps[keep:]:
        old.unlink()
        print(f"pruned {old.name}")


# ---------- commands ----------

def cmd_episodes():
    tts_cfg = CFG["tts"]
    made = 0
    for script in sorted(SCRIPTS.glob("*.md")):
        try:
            dt.date.fromisoformat(script.stem)
        except ValueError:
            continue
        out = EPISODES / f"{script.stem}.mp3"
        if out.exists():
            continue
        text = speakable(script.read_text())
        print(f"rendering {script.name}: {len(text)} chars with {tts_cfg['model']}")
        render(text, tts_cfg["model"], tts_cfg["voice"], out, tts_cfg["max_chunk_chars"])
        print(f"  -> {out.name} ({duration(out)}s)")
        made += 1
    prune()
    build_feed()
    print(f"done, {made} new episode(s)")


def cmd_samples():
    sample_text = speakable((SCRIPTS / "sample.md").read_text())
    rows = []
    for i, s in enumerate(CFG["samples"]):
        name = f"{i + 1:02d}-" + re.sub(r"[^a-z0-9]+", "-", s["label"].lower()).strip("-") + ".mp3"
        out = SAMPLES / name
        try:
            if not out.exists():
                print(f"sampling {s['label']}")
                render(sample_text, s["model"], s["voice"], out, CFG["tts"]["max_chunk_chars"])
            rows.append((s, name, None))
        except Exception as e:  # keep going so one bad model doesn't sink the page
            annotate(f"sample {s['label']} failed: {str(e)[:300]}")
            rows.append((s, None, str(e)[:200]))
    cards = []
    for s, name, err in rows:
        body = (f'<audio controls preload="none" src="{name}"></audio>' if name
                else f'<p class="err">Failed: {html.escape(err)}</p>')
        cards.append(f"""<section><h2>{html.escape(s['label'])}</h2>
<p class="m">{html.escape(s['model'])} &middot; {html.escape(s['voice'])}</p>{body}</section>""")
    page = f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Voice Sampler</title><style>
:root{{--bg:#f7f5f0;--fg:#1d1b18;--mute:#6b665d;--card:#fff;--line:#e4dfd5;--err:#a33}}
@media (prefers-color-scheme:dark){{:root{{--bg:#151412;--fg:#ece8e0;--mute:#9a948a;--card:#1f1d1a;--line:#2e2b27;--err:#e88}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:16px/1.45 -apple-system,system-ui,sans-serif;padding:24px 16px 48px}}
main{{max-width:560px;margin:auto}}h1{{font-size:26px;margin:0 0 4px}}.lede{{color:var(--mute);margin:0 0 20px}}
section{{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 16px;margin:0 0 12px}}
h2{{font-size:17px;margin:0}}.m{{color:var(--mute);font-size:13px;margin:2px 0 10px}}audio{{width:100%}}.err{{color:var(--err);font-size:14px}}
</style></head><body><main><h1>Voice Sampler</h1>
<p class="lede">Same script, every candidate. Pick the one you'd want in your ears at 6:30am.</p>
{''.join(cards)}</main></body></html>"""
    SAMPLES.mkdir(parents=True, exist_ok=True)
    (SAMPLES / "index.html").write_text(page)
    build_feed()


def cmd_all():
    """Samples are cached per file, so this only spends money on new ones."""
    cmd_samples()
    cmd_episodes()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    {"all": cmd_all, "episodes": cmd_episodes, "samples": cmd_samples, "feed": build_feed}[cmd]()

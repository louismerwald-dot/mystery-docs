"""
mystery-docs: Auto-generated 7-10 min documentary videos about unexplained
historical mysteries, strange phenomena, and lost places.

Daily pipeline:
  1. Discover candidate mysteries from Reddit (UnresolvedMysteries, AskHistorians,
     HighStrangeness) + Wikipedia "Unexplained phenomena" / "Mysteries" categories
  2. Filter out content that triggers YouTube demonetization (recent crime,
     violence, real-living-people conspiracies)
  3. Gemini ranks safe candidates and picks the most "video-worthy" one
  4. Gemini writes a slow-burn cinematic narration (~1100 words = 7-8 min)
  5. edge-tts narrates with deep storyteller voice
  6. For each ~10s segment, Gemini extracts a moody Pexels query
  7. Pexels API fetches atmospheric stock footage
  8. FFmpeg assembles 1920x1080 with bouncy captions and dark BGM
  9. YouTube uploads as Public, History/Education category

Required env vars: GEMINI_API_KEY, PEXELS_API_KEY, YT_CLIENT_ID,
YT_CLIENT_SECRET, YT_REFRESH_TOKEN
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import os
import random
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote_plus

import edge_tts
import requests
import yaml
from google import genai
from google.genai import types as genai_types
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

ROOT = Path(__file__).parent.resolve()
WORK = ROOT / "work"
STATE_FILE = ROOT / "state.json"
CONFIG_FILE = ROOT / "config.yaml"

# ----------------------------- config + state -----------------------------

def load_config() -> dict:
    with CONFIG_FILE.open() as f:
        return yaml.safe_load(f)

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"used_topics": []}

def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ----------------------------- safety filter -----------------------------

# Words that strongly indicate content YouTube will demonetize or strike.
# We filter at the candidate level so Gemini never even sees risky picks.
DEMONETIZATION_BLOCKLIST = re.compile(
    r"\b("
    r"murder|murdered|killer|killing|killed|massacre|"
    r"rape|raped|assault|abuse|abused|"
    r"missing\s+(child|girl|boy|teen)|kidnapp|abducted|"
    r"suicide|self-?harm|"
    r"jeffrey\s+epstein|harvey\s+weinstein|"
    r"madeleine\s+mccann|jonbenet|"
    r"zodiac\s+killer|gary\s+ridgway|jeffrey\s+dahmer|"
    r"ted\s+bundy|charles\s+manson|"
    r"qanon|stop\s+the\s+steal|election\s+frau|"
    r"anti-?vax|covid\s+conspirac|"
    r"holocaust\s+denial|9/?11\s+inside\s+job"
    r")\b",
    re.I,
)

def is_safe(title: str, summary: str = "") -> bool:
    return DEMONETIZATION_BLOCKLIST.search(title + " " + summary) is None

# ----------------------------- topic discovery -----------------------------

@dataclass
class TopicCandidate:
    title: str
    url: str
    source: str
    score: int = 0
    summary: str = ""

REDDIT_SUBS = [
    ("UnresolvedMysteries", "month"),
    ("AskHistorians", "month"),
    ("HighStrangeness", "month"),
    ("UnsolvedMysteries", "month"),
    ("conspiracy", "month"),  # filtered hard for safe topics only
    ("Lost_Architecture", "year"),
    ("AbandonedPorn", "year"),
]

def fetch_reddit(subreddit: str, period: str, limit: int = 25) -> list[TopicCandidate]:
    """Top posts from a subreddit's public JSON. No API key needed."""
    out: list[TopicCandidate] = []
    try:
        resp = requests.get(
            f"https://www.reddit.com/r/{subreddit}/top.json?t={period}&limit={limit}",
            headers={"User-Agent": "mystery-docs/1.0"},
            timeout=20,
        )
        if resp.status_code != 200:
            return out
        for post in resp.json().get("data", {}).get("children", []):
            d = post.get("data", {})
            title = d.get("title", "")
            if not title:
                continue
            summary = (d.get("selftext") or "")[:600]
            if not is_safe(title, summary):
                continue
            out.append(TopicCandidate(
                title=title,
                url=f"https://reddit.com{d.get('permalink', '')}",
                source=f"reddit/{subreddit}",
                score=d.get("score", 0),
                summary=summary,
            ))
    except Exception as e:
        print(f"[warn] reddit /r/{subreddit} fetch failed: {e}", file=sys.stderr)
    return out

# Wikipedia category members give us a HUGE evergreen pool. No API key needed.
# These categories contain hundreds of mystery articles each.
WIKI_CATEGORIES = [
    "Unexplained_phenomena",
    "Mysteries",
    "Lost_cities",
    "Cryptids",
    "Unsolved_problems_in_archaeology",
    "Hoaxes",
    "Mysterious_disappearances",  # filtered for safe ones
    "Out-of-place_artifacts",
    "Ancient_mysteries",
    "Anomalous_phenomena",
]

def fetch_wikipedia_category(category: str, limit: int = 50) -> list[TopicCandidate]:
    """Wikipedia API: list pages in a category."""
    out: list[TopicCandidate] = []
    try:
        url = "https://en.wikipedia.org/w/api.php"
        params = {
            "action": "query",
            "format": "json",
            "list": "categorymembers",
            "cmtitle": f"Category:{category}",
            "cmlimit": limit,
            "cmtype": "page",
        }
        resp = requests.get(url, params=params, timeout=20,
                            headers={"User-Agent": "mystery-docs/1.0"})
        if resp.status_code != 200:
            return out
        for m in resp.json().get("query", {}).get("categorymembers", []):
            title = m.get("title", "")
            if not title or ":" in title:  # skip Talk: / File: pages
                continue
            if not is_safe(title):
                continue
            page_url = f"https://en.wikipedia.org/wiki/{quote_plus(title.replace(' ', '_'))}"
            out.append(TopicCandidate(
                title=title,
                url=page_url,
                source=f"wikipedia/{category}",
            ))
    except Exception as e:
        print(f"[warn] wikipedia category {category} fetch failed: {e}", file=sys.stderr)
    return out

def fetch_wikipedia_summary(title: str) -> str:
    """Quick Wikipedia summary for context. Used before Gemini ranks."""
    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote_plus(title.replace(' ', '_'))}"
        r = requests.get(url, timeout=15, headers={"User-Agent": "mystery-docs/1.0"})
        if r.status_code == 200:
            return (r.json().get("extract") or "")[:800]
    except Exception:
        pass
    return ""

def gather_candidates(cfg: dict) -> list[TopicCandidate]:
    cands: list[TopicCandidate] = []
    for sub, period in REDDIT_SUBS:
        cands += fetch_reddit(sub, period, 25)
    for cat in cfg.get("wikipedia_categories", WIKI_CATEGORIES):
        cands += fetch_wikipedia_category(cat, 40)
    # de-dupe by title
    seen: set[str] = set()
    deduped: list[TopicCandidate] = []
    for c in cands:
        key = c.title.lower().strip()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    return deduped

# ----------------------------- gemini helpers -----------------------------

def _gemini():
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])

def _gemini_json(prompt: str, *, temperature: float = 0.6) -> dict | list:
    resp = _gemini().models.generate_content(
        model="gemini-2.5-flash",
        contents=prompt,
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=temperature,
        ),
    )
    return json.loads(resp.text)

# ----------------------------- topic selection -----------------------------

PICK_PROMPT = """You're picking the next mystery documentary topic for a YouTube channel.

The CORRECT pick is:
  - An unexplained historical event, lost place, ancient enigma, strange phenomenon, or weird disappearance from at least 30+ years ago (the older the safer)
  - Self-contained: a viewer with zero context can follow a 7-min explainer
  - Visually narratable with stock footage of: forests, oceans, ruins, old documents, candles, fog, mountains, ancient places, abandoned buildings
  - NOT in the "already used" list below
  - NOT about: recent murders, identifiable living people's crimes, missing children, modern conspiracy theories, COVID, elections, recent politics. We need topics that are atmospheric and old.

Examples of strong picks: Roanoke Colony, the Dyatlov Pass incident, the Voynich Manuscript, the Antikythera Mechanism, the Wow! Signal, the Lost Colony, the Piri Reis Map, Tunguska event, lost city of Atlantis, the Nazca Lines, Easter Island moai, the Bog Bodies of Europe, the Phaistos Disc, the Aluxes of Mexico, the Great Emu War, the Princes in the Tower, the death of Tutankhamun, the disappearance of Amelia Earhart, the lost Roman legions, the Year Without a Summer.

Return STRICT JSON:
{{
  "winner_index": <int>,
  "topic": "<the topic title rewritten as a punchy headline, max 80 chars - e.g. 'What Happened to the Roanoke Colony?'>",
  "angle": "<one sentence on the unique angle this video will take>",
  "reason": "<one sentence on why this topic will perform>"
}}

Already used (don't repeat or close paraphrase):
{used}

Candidates (index | source | score | title):
{candidates}
"""

def pick_topic(candidates: list[TopicCandidate], used_topics: list[str]) -> dict:
    lines = []
    for i, c in enumerate(candidates[:120]):
        lines.append(f"{i} | {c.source} | {c.score} | {c.title}")
    prompt = PICK_PROMPT.format(
        used="\n".join(f"- {t}" for t in used_topics[-100:]) or "(none yet)",
        candidates="\n".join(lines),
    )
    pick = _gemini_json(prompt, temperature=0.5)
    idx = int(pick["winner_index"])
    chosen = candidates[idx]
    # If it's a Wikipedia article, fetch the summary as research material
    research = ""
    if chosen.source.startswith("wikipedia/"):
        research = fetch_wikipedia_summary(chosen.title)
    elif chosen.summary:
        research = chosen.summary
    return {
        "topic": pick["topic"],
        "angle": pick["angle"],
        "reason": pick["reason"],
        "source_title": chosen.title,
        "source_url": chosen.url,
        "research": research,
    }

# ----------------------------- script writing -----------------------------

SCRIPT_PROMPT = """You are writing a documentary narration for a YouTube mystery channel.

Topic: {topic}
Angle: {angle}
Source title: {source_title}

Research material (use as factual ground truth, don't just summarize):
{research}

Voice and tone:
  - Slow-burn cinematic. Like "Bedtime Stories" or "Lemmino" - calm, measured, ominous
  - Builds atmosphere with concrete sensory details (the cold, the silence, the dim light)
  - Treats the audience as intelligent. No "what would YOU have done" filler
  - Never sensationalizes. Never says "you won't believe..." or "what they found will shock you"
  - Slight wry detachment is welcome - this is a storyteller who's seen weirder things

Constraints:
  - Total length: {target_words} words (~{target_minutes} minutes at 150 wpm)
  - Open with a hook in the first 2 sentences (a vivid scene or unsettling fact)
  - 4-6 sections that escalate naturally. Use phrases like "But the strangest part was still to come" or "What happened next has never been satisfactorily explained"
  - Include factually accurate details: dates, names, places, numbers
  - End on an open question or unresolved note - never wrap with a definitive answer
  - NO em-dashes (read poorly aloud)
  - NO bullet points, headers, markdown
  - NO references to "this video", "today", "subscribe", "let me know in the comments"
  - DO NOT speculate wildly. Stick to documented theories. If the mystery has a most-likely mundane explanation, mention it but don't dismiss the strangeness

Return STRICT JSON:
{{
  "narration": "<the full narration text, plain prose>",
  "title": "<YouTube title, <=70 chars. Use formats like 'What Happened to X', 'The Strange Case of Y', 'Nobody Can Explain Z'. No clickbait emojis>",
  "description": "<2-paragraph YouTube description, ~120 words, ending with 'Sources:' and the source URL>",
  "tags": ["<10-12 lowercase tags relevant to mystery/history/unexplained>"]
}}
"""

def write_script(picked: dict, target_minutes: float) -> dict:
    target_words = int(target_minutes * 150)
    prompt = SCRIPT_PROMPT.format(
        topic=picked["topic"],
        angle=picked["angle"],
        source_title=picked["source_title"],
        research=picked.get("research", "(no additional research available)"),
        target_words=target_words,
        target_minutes=target_minutes,
    )
    return _gemini_json(prompt, temperature=0.75)

# ----------------------------- TTS -----------------------------

async def _synth_async(text: str, voice: str, out_path: Path, srt_path: Path) -> None:
    # Slightly slower than default for storyteller pacing.
    # FIX APPLIED HERE: Request explicit WordBoundaries.
    communicate = edge_tts.Communicate(text, voice, rate="-5%", boundary="WordBoundary")
    submaker = edge_tts.SubMaker()
    with out_path.open("wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            # FIX APPLIED HERE: Accept both boundary types for total resilience.
            elif chunk["type"] in ["WordBoundary", "SentenceBoundary"]:
                submaker.feed(chunk)
    srt_path.write_text(submaker.get_srt(), encoding="utf-8")

def synth_narration(text: str, voice: str, out_path: Path, srt_path: Path) -> None:
    asyncio.run(_synth_async(text, voice, out_path, srt_path))

# ----------------------------- SRT parsing for segment alignment -----------------------------

@dataclass
class Cue:
    start: float
    end: float
    text: str

def parse_srt(path: Path) -> list[Cue]:
    raw = path.read_text(encoding="utf-8").strip()
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", raw)
    for block in blocks:
        lines = [l for l in block.splitlines() if l.strip()]
        if len(lines) < 2:
            continue
        tc_line = next((l for l in lines if "-->" in l), None)
        if not tc_line:
            continue
        m = re.match(
            r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)",
            tc_line,
        )
        if not m:
            continue
        h1, m1, s1, ms1, h2, m2, s2, ms2 = map(int, m.groups())
        start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000.0
        end = h2 * 3600 + m2 * 60 + s2 + ms2 / 1000.0
        idx = lines.index(tc_line)
        text = " ".join(lines[idx + 1:]).strip()
        if text:
            cues.append(Cue(start, end, text))
    return cues

def chunk_into_segments(cues: list[Cue], target_seconds: float = 10.0) -> list[Cue]:
    if not cues:
        return []
    out: list[Cue] = []
    cur_start = cues[0].start
    cur_text: list[str] = []
    cur_end = cues[0].end
    for c in cues:
        cur_text.append(c.text)
        cur_end = c.end
        if cur_end - cur_start >= target_seconds:
            out.append(Cue(cur_start, cur_end, " ".join(cur_text)))
            cur_start = cur_end
            cur_text = []
    if cur_text:
        if out:
            last = out[-1]
            out[-1] = Cue(last.start, cur_end, last.text + " " + " ".join(cur_text))
        else:
            out.append(Cue(cur_start, cur_end, " ".join(cur_text)))
    return out

# ----------------------------- Pexels footage -----------------------------

QUERY_PROMPT = """For each segment of a mystery documentary narration, return a short stock-footage search query (2-4 words) that visually matches the MOOD and TOPIC.

Bias toward atmospheric, cinematic, slightly ominous footage. Examples of good queries:
  "foggy forest path", "ocean storm waves", "ancient ruins", "candlelight darkness",
  "abandoned building", "snowy mountain peak", "old paper documents", "dark cave entrance",
  "moonlit night sky", "deep ocean underwater", "ancient stone carvings", "rain on window",
  "empty corridor", "library old books", "lone figure walking", "remote cabin", "cliff edge mist"

If the segment mentions a specific place/era (Egypt, Roman, medieval, jungle, arctic), use that.
Avoid bright/cheerful queries. This is mystery content - we want shadows, fog, isolation.

Return STRICT JSON:
{{"queries": ["<query for segment 0>", ...]}}

Segments:
{segments}
"""

def queries_for_segments(segments: list[Cue]) -> list[str]:
    seg_lines = [f"{i}: {s.text}" for i, s in enumerate(segments)]
    prompt = QUERY_PROMPT.format(segments="\n".join(seg_lines))
    data = _gemini_json(prompt, temperature=0.5)
    qs = data.get("queries", [])
    while len(qs) < len(segments):
        qs.append("foggy forest")
    return qs[:len(segments)]

PEXELS_FALLBACKS = [
    "foggy forest", "ocean storm", "ancient ruins", "candle darkness",
    "rain window", "abandoned building", "moonlit night", "old paper",
]

def pexels_video_for_query(query: str, min_duration: float, key: str) -> str | None:
    headers = {"Authorization": key}
    url = "https://api.pexels.com/videos/search"
    params = {"query": query, "per_page": 15, "orientation": "landscape", "size": "medium"}
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=20)
        if resp.status_code != 200:
            return None
        videos = resp.json().get("videos", [])
        random.shuffle(videos)
        for v in videos:
            if v.get("duration", 0) < min_duration:
                continue
            files = sorted(
                (f for f in v.get("video_files", []) if f.get("file_type") == "video/mp4"),
                key=lambda f: abs((f.get("width") or 0) - 1920),
            )
            for f in files:
                w = f.get("width") or 0
                if 1280 <= w <= 2560:
                    return f.get("link")
            if files:
                return files[0].get("link")
    except Exception as e:
        print(f"[warn] pexels search '{query}' failed: {e}", file=sys.stderr)
    return None

def download_clip(url: str, dest: Path) -> bool:
    try:
        with requests.get(url, stream=True, timeout=120) as r:
            if r.status_code != 200:
                return False
            with dest.open("wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 16):
                    if chunk:
                        f.write(chunk)
        return dest.stat().st_size > 50_000
    except Exception as e:
        print(f"[warn] download failed: {e}", file=sys.stderr)
        return False

def gather_footage(
    segments: list[Cue], queries: list[str], pexels_key: str, footage_dir: Path
) -> list[Path]:
    paths: list[Path] = []
    used_urls: set[str] = set()
    for i, (seg, query) in enumerate(zip(segments, queries)):
        duration = max(2.0, seg.end - seg.start)
        clip_path = footage_dir / f"seg_{i:03d}.mp4"
        url: str | None = None
        for q in [query] + PEXELS_FALLBACKS:
            cand = pexels_video_for_query(q, duration, pexels_key)
            if cand and cand not in used_urls:
                url = cand
                used_urls.add(cand)
                break
        if not url:
            if paths:
                shutil.copy(paths[-1], clip_path)
                paths.append(clip_path)
                print(f"    [{i}] no fresh clip; reused previous", file=sys.stderr)
                continue
            raise RuntimeError(f"No Pexels footage available for segment {i}")
        if not download_clip(url, clip_path):
            if paths:
                shutil.copy(paths[-1], clip_path)
                paths.append(clip_path)
                continue
            raise RuntimeError(f"Failed to download Pexels clip for segment {i}")
        paths.append(clip_path)
        print(f"    [{i+1}/{len(segments)}] '{query}' -> {clip_path.name}")
    return paths

# ----------------------------- captions (ASS) -----------------------------

def build_caption_file(cues: list[Cue], out_path: Path, chunk_words: int = 4) -> None:
    """Subtle storyteller-style captions. Smaller, less aggressive than tech-style."""
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,64,&H00FFFFFF,&H00000000,&H80000000,1,0,1,4,2,2,80,80,90,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    def ts(t: float) -> str:
        h = int(t // 3600); t -= h * 3600
        m = int(t // 60);   t -= m * 60
        s = int(t)
        cs = int(round((t - s) * 100))
        if cs == 100:
            s += 1; cs = 0
        return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"

    lines = [header]
    for i in range(0, len(cues), chunk_words):
        group = cues[i:i + chunk_words]
        if not group:
            continue
        text = " ".join(c.text for c in group).upper()
        text = text.replace("\\", "\\\\").replace("{", "(").replace("}", ")")
        lines.append(
            f"Dialogue: 0,{ts(group[0].start)},{ts(group[-1].end)},Default,,0,0,0,,{text}"
        )
    out_path.write_text("\n".join(lines), encoding="utf-8")

# ----------------------------- ffmpeg assembly -----------------------------

def normalize_clip(src: Path, dst: Path, duration: float) -> None:
    """Trim/loop src to exactly `duration` seconds, scaled to 1920x1080, no audio.
    Slight darken filter to keep moody atmosphere consistent."""
    vf = (
        f"scale=1920:1080:force_original_aspect_ratio=increase,"
        f"crop=1920:1080,"
        f"setsar=1,"
        f"eq=brightness=-0.04:saturation=0.9,"  # slight cinematic darken
        f"tpad=stop_mode=clone:stop_duration={duration}"
    )
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-stream_loop", "-1", "-i", str(src),
        "-t", f"{duration:.3f}",
        "-vf", vf,
        "-an",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        str(dst),
    ]
    subprocess.run(cmd, check=True)

def concat_normalized(clips: list[Path], dst: Path) -> None:
    list_path = dst.parent / "concat.txt"
    list_path.write_text("\n".join(f"file '{c.as_posix()}'" for c in clips))
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "concat", "-safe", "0", "-i", str(list_path),
        "-c", "copy", str(dst),
    ]
    subprocess.run(cmd, check=True)

def mux_final(
    video_silent: Path, narration: Path, captions_ass: Path,
    bgm: Path | None, dst: Path,
) -> None:
    inputs = ["-i", str(video_silent), "-i", str(narration)]
    if bgm and bgm.exists():
        inputs += ["-stream_loop", "-1", "-i", str(bgm)]

    vf = f"ass={captions_ass.as_posix()}"

    if bgm and bgm.exists():
        # narration loud + bgm at 8% (ominous tracks need to sit lower)
        filter_complex = (
            f"[0:v]{vf}[v];"
            f"[1:a]volume=1.0[a1];"
            f"[2:a]volume=0.08[a2];"
            f"[a1][a2]amix=inputs=2:duration=first:dropout_transition=2[a]"
        )
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        filter_complex = f"[0:v]{vf}[v]"
        maps = ["-map", "[v]", "-map", "1:a"]

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        *inputs,
        "-filter_complex", filter_complex,
        *maps,
        "-shortest",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(dst),
    ]
    subprocess.run(cmd, check=True)

# ----------------------------- youtube upload -----------------------------

def youtube_client():
    creds = Credentials(
        token=None,
        refresh_token=os.environ["YT_REFRESH_TOKEN"],
        client_id=os.environ["YT_CLIENT_ID"],
        client_secret=os.environ["YT_CLIENT_SECRET"],
        token_uri="https://oauth2.googleapis.com/token",
        scopes=["https://www.googleapis.com/auth/youtube.upload"],
    )
    creds.refresh(GoogleAuthRequest())
    return build("youtube", "v3", credentials=creds)

def upload_video(
    yt, file_path: Path, title: str, description: str, tags: list[str], category_id: str = "27",
) -> str:
    # 27 = Education (good for History/Mystery), 22 = People & Blogs as fallback
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:4900],
            "tags": [t.lower()[:30] for t in tags][:15],
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": "public",
            "selfDeclaredMadeForKids": False,
        },
    }
    media = MediaFileUpload(str(file_path), mimetype="video/mp4", resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    while response is None:
        _status, response = req.next_chunk()
    return response["id"]

# ----------------------------- driver -----------------------------

def main() -> int:
    WORK.mkdir(exist_ok=True)
    cfg = load_config()
    state = load_state()

    target_minutes = float(cfg.get("target_minutes", 8.0))
    voice = cfg.get("voice", "en-US-AndrewMultilingualNeural")
    pexels_key = os.environ["PEXELS_API_KEY"]

    print("[1/8] Gathering mystery candidates from Reddit + Wikipedia...")
    candidates = gather_candidates(cfg)
    print(f"      {len(candidates)} safe candidates")
    if not candidates:
        print("[error] no candidates", file=sys.stderr)
        return 1

    print("[2/8] Picking topic with Gemini...")
    picked = pick_topic(candidates, state.get("used_topics", []))
    print(f"      Chose: {picked['topic']}")
    print(f"      Angle: {picked['angle']}")

    print(f"[3/8] Writing ~{target_minutes}-min cinematic script...")
    script = write_script(picked, target_minutes)
    narration_text = script["narration"]
    word_count = len(narration_text.split())
    print(f"      {word_count} words ({word_count / 150:.1f} min @ 150 wpm)")
    print(f"      Title: {script['title']}")

    # FIX APPLIED HERE: Deprecated UTC warning fixed
    run_dir = WORK / dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "script.txt").write_text(narration_text, encoding="utf-8")

    print("[4/8] Synthesizing narration (edge-tts, slow storyteller pace)...")
    narration_mp3 = run_dir / "narration.mp3"
    word_srt = run_dir / "words.srt"
    synth_narration(narration_text, voice, narration_mp3, word_srt)
    cues = parse_srt(word_srt)
    if not cues:
        print("[error] TTS returned no word cues", file=sys.stderr)
        return 1
    total_audio = cues[-1].end
    print(f"      narration {total_audio:.1f}s, {len(cues)} word cues")

    print("[5/8] Chunking + getting Pexels queries...")
    segments = chunk_into_segments(cues, target_seconds=cfg.get("segment_seconds", 10.0))
    print(f"      {len(segments)} visual segments")
    queries = queries_for_segments(segments)

    print("[6/8] Downloading Pexels footage...")
    footage_dir = run_dir / "footage"
    footage_dir.mkdir(exist_ok=True)
    raw_clips = gather_footage(segments, queries, pexels_key, footage_dir)

    print("[7/8] Normalizing clips + assembling...")
    norm_dir = run_dir / "normalized"
    norm_dir.mkdir(exist_ok=True)
    norm_clips: list[Path] = []
    for i, (clip, seg) in enumerate(zip(raw_clips, segments)):
        norm = norm_dir / f"n_{i:03d}.mp4"
        normalize_clip(clip, norm, seg.end - seg.start)
        norm_clips.append(norm)
    silent = run_dir / "silent.mp4"
    concat_normalized(norm_clips, silent)

    print("      Building captions + final mux...")
    captions = run_dir / "captions.ass"
    build_caption_file(cues, captions)

    bgm = ROOT / "assets" / "bgm.mp3"
    final = run_dir / "final.mp4"
    mux_final(silent, narration_mp3, captions, bgm if bgm.exists() else None, final)
    print(f"      final video: {final.stat().st_size / 1e6:.1f} MB")

    print("[8/8] Uploading to YouTube...")
    yt = youtube_client()
    description = (
        f"{script['description']}\n\n"
        f"Source material: {picked['source_url']}\n"
    )
    vid_id = upload_video(
        yt, final,
        title=script["title"],
        description=description,
        tags=script.get("tags", []),
    )
    url = f"https://youtu.be/{vid_id}"
    print(f"      uploaded -> {url}")

    used = state.get("used_topics", [])
    used.append(picked["topic"])
    state["used_topics"] = used[-200:]
    save_state(state)

    shutil.rmtree(run_dir, ignore_errors=True)
    print("[done]")
    return 0

if __name__ == "__main__":
    sys.exit(main())

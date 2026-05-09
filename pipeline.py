"""
mystery-docs: Auto-generated 7-10 min documentary videos about unexplained
historical mysteries, strange phenomena, and lost places.

Upgraded with YouTube Creator optimizations: Cinematic grading, audio ducking,
retention-focused script prompts, and modern typography.
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
    if CONFIG_FILE.exists():
        with CONFIG_FILE.open() as f:
            return yaml.safe_load(f) or {}
    return {}

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"used_topics": []}

def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))

# ----------------------------- safety filter -----------------------------

DEMONETIZATION_BLOCKLIST = re.compile(
    r"\b(murder|murdered|killer|killing|killed|massacre|rape|raped|assault|abuse|abused|missing\s+(child|girl|boy|teen)|kidnapp|abducted|suicide|self-?harm|jeffrey\s+epstein|harvey\s+weinstein|madeleine\s+mccann|jonbenet|zodiac\s+killer|gary\s+ridgway|jeffrey\s+dahmer|ted\s+bundy|charles\s+manson|qanon|stop\s+the\s+steal|election\s+frau|anti-?vax|covid\s+conspirac|holocaust\s+denial|9/?11\s+inside\s+job)\b",
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
    ("Lost_Architecture", "year"),
]

WIKI_CATEGORIES = [
    "Unexplained_phenomena",
    "Mysteries",
    "Lost_cities",
    "Cryptids",
    "Unsolved_problems_in_archaeology",
    "Hoaxes",
    "Out-of-place_artifacts",
    "Ancient_mysteries",
]

def fetch_reddit(subreddit: str, period: str, limit: int = 25) -> list[TopicCandidate]:
    out: list[TopicCandidate] = []
    try:
        resp = requests.get(
            f"https://www.reddit.com/r/{subreddit}/top.json?t={period}&limit={limit}",
            headers={"User-Agent": "mystery-docs/2.0"},
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

def fetch_wikipedia_category(category: str, limit: int = 50) -> list[TopicCandidate]:
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
                            headers={"User-Agent": "mystery-docs/2.0"})
        if resp.status_code != 200:
            return out
        for m in resp.json().get("query", {}).get("categorymembers", []):
            title = m.get("title", "")
            if not title or ":" in title:
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
    try:
        url = f"https://en.wikipedia.org/api/rest_v1/page/summary/{quote_plus(title.replace(' ', '_'))}"
        r = requests.get(url, timeout=15, headers={"User-Agent": "mystery-docs/2.0"})
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

_GEMINI_CLIENT = None

def _gemini():
    global _GEMINI_CLIENT
    if _GEMINI_CLIENT is None:
        _GEMINI_CLIENT = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _GEMINI_CLIENT

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

PICK_PROMPT = """You are an elite YouTube strategist running a highly successful Mystery/Documentary channel (think Lemmino, Wendigoon, or MrBallen).
Pick the most viral, click-worthy historical mystery from the list below.

Rules for picking:
1. It MUST have "curiosity gap" potential (an event so bizarre that viewers HAVE to know what happened).
2. It must be visually narratable with atmospheric B-roll (ruins, oceans, documents, forests, creepy cabins).
3. Avoid anything political, recent, or highly controversial. Stick to classic, eerie, unexplained history.

Return STRICT JSON:
{{
  "winner_index": <int>,
  "topic": "<Rewrite the title to be a highly clickable, intriguing YouTube title (max 70 chars). E.g., 'The Lost City Nobody Can Find'>",
  "angle": "<One sentence explaining the psychological hook of the story>",
  "reason": "<One sentence explaining why this will get high viewer retention>"
}}

Already used (DO NOT PICK):
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
    pick = _gemini_json(prompt, temperature=0.7)
    idx = int(pick["winner_index"])
    chosen = candidates[idx]
    
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

SCRIPT_PROMPT = """You are a master scriptwriter for a top-tier YouTube mystery channel. 
Your goal is MAXIMUM AUDIENCE RETENTION. 

Topic: {topic}
Angle: {angle}
Factual Basis: {research}

Scripting Rules:
1. THE HOOK (First 15 seconds): Start *in media res*. Describe a chilling sensory detail or the exact moment the mystery began. DO NOT say "Today we are looking at..."
2. THE BUILD-UP: Treat the audience as intelligent. Feed them clues slowly. Use "open loops" (e.g., "But what they found inside made the situation infinitely worse.")
3. PACING: Short, punchy sentences. Pause for effect. 
4. NO FILLER: Zero fluff, zero "Make sure to subscribe" lines. 
5. THE CLIMAX & OUTRO: Present the leading theories, but leave the audience with a lingering, unsettling question. 
6. NO MARKDOWN. NO HEADERS. ONLY PLAIN PROSE.

Length: Exactly {target_words} words.

Return STRICT JSON:
{{
  "narration": "<The full, captivating narration text>",
  "title": "<A viral, high-CTR YouTube title, max 65 chars>",
  "description": "<A 2-paragraph compelling description, ending with 'Sources: {source_url}'>",
  "tags": ["<15 highly relevant, high-search-volume tags>"]
}}
"""

def write_script(picked: dict, target_minutes: float) -> dict:
    target_words = int(target_minutes * 150)
    prompt = SCRIPT_PROMPT.format(
        topic=picked["topic"],
        angle=picked["angle"],
        source_title=picked["source_title"],
        research=picked.get("research", "(no research available)"),
        target_words=target_words,
        source_url=picked["source_url"]
    )
    return _gemini_json(prompt, temperature=0.85)

# ----------------------------- TTS -----------------------------

async def _synth_async(text: str, voice: str, out_path: Path, srt_path: Path) -> None:
    communicate = edge_tts.Communicate(text, voice, rate="-8%", pitch="-2Hz", boundary="WordBoundary")
    submaker = edge_tts.SubMaker()
    with out_path.open("wb") as f:
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
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

def chunk_into_segments(cues: list[Cue], target_seconds: float = 6.5) -> list[Cue]:
    # Faster pacing for YouTube: Changed target from 10s to 6.5s
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

QUERY_PROMPT = """You are a Director of Photography selecting cinematic B-roll for a mystery documentary.
For each narration segment, provide a highly specific search query AND a mood-based fallback.

Rules (2-4 words max):
1. 'Primary' must be literal objects or places (e.g., "vintage map", "typewriter", "snowy tracks", "old wooden ship", "abandoned hospital").
2. 'Fallback' must be a broader, highly atmospheric vibe (e.g., "dark fog", "stormy sea", "creepy shadows").
3. DO NOT use proper nouns or character names. 

Return STRICT JSON:
{{"segments": [
  {{"primary": "snowy mountain footprints", "fallback": "winter blizzard"}},
  {{"primary": "old newspaper clipping", "fallback": "dusty library books"}}
]}}

Segments:
{segments}
"""

def queries_for_segments(segments: list[Cue]) -> list[dict]:
    seg_lines = [f"{i}: {s.text}" for i, s in enumerate(segments)]
    prompt = QUERY_PROMPT.format(segments="\n".join(seg_lines))
    data = _gemini_json(prompt, temperature=0.5)
    qs = data.get("segments", [])
    while len(qs) < len(segments):
        qs.append({"primary": "dark foggy forest", "fallback": "night sky moon"})
    return qs[:len(segments)]

PEXELS_FALLBACKS = [
    "foggy woods", "stormy ocean", "dusty library", "candlelight",
    "rain window", "creepy abandoned building", "moonlight clouds", "old compass",
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
        pass
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
    except Exception:
        return False

def gather_footage(
    segments: list[Cue], queries: list[dict], pexels_key: str, footage_dir: Path
) -> list[Path]:
    paths: list[Path] = []
    used_urls: set[str] = set()
    for i, (seg, query_dict) in enumerate(zip(segments, queries)):
        duration = max(2.0, seg.end - seg.start)
        clip_path = footage_dir / f"seg_{i:03d}.mp4"
        url: str | None = None
        
        search_list = [query_dict.get("primary", ""), query_dict.get("fallback", "")] + PEXELS_FALLBACKS
        search_list = [q for q in search_list if q]
        
        for q in search_list:
            cand = pexels_video_for_query(q, duration, pexels_key)
            if cand and cand not in used_urls:
                url = cand
                used_urls.add(cand)
                break
                
        if not url:
            if paths:
                shutil.copy(paths[-1], clip_path)
                paths.append(clip_path)
                continue
            raise RuntimeError(f"No footage available for segment {i}")
            
        if not download_clip(url, clip_path):
            if paths:
                shutil.copy(paths[-1], clip_path)
                paths.append(clip_path)
                continue
            raise RuntimeError(f"Failed download segment {i}")
            
        paths.append(clip_path)
        print(f"      [{i+1}/{len(segments)}] Searched: '{search_list[0]}'")
        
    return paths

# ----------------------------- captions (ASS) -----------------------------

def build_caption_file(cues: list[Cue], out_path: Path, chunk_words: int = 5) -> None:
    # YouTube Creator Typography: Bold, Yellow, Center-Screen, Heavy Shadow
    header = """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,Arial Black,75,&H0000FFFF,&H00000000,&H99000000,-1,0,1,3,4,5,80,80,120,1

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
    # Creator Visuals: High contrast, desaturated, slight grain/noise for grittiness
    vf = f"scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1,eq=brightness=-0.05:contrast=1.1:saturation=0.65,noise=alls=8:allf=t+u,tpad=stop_mode=clone:stop_duration={duration}"
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
        # Creator Audio: BGM ducks down to 3% when narrator speaks, rises to 15% during silent pauses
        filter_complex = f"[0:v]{vf}[v];[1:a]volume=1.0[a1];[2:a]volume=0.15[a2];[a2][a1]sidechaincompress=threshold=0.08:ratio=4:attack=5:release=50[bgm_ducked];[a1][bgm_ducked]amix=inputs=2:duration=first:dropout_transition=2[a]"
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
    voice = cfg.get("voice", "en-GB-RyanNeural") # Switched to a slightly deeper, grittier UK voice for mystery vibe
    pexels_key = os.environ["PEXELS_API_KEY"]

    print("[1/8] Gathering mysteries like a YouTube Strategist...")
    candidates = gather_candidates(cfg)
    if not candidates:
        print("[error] no candidates", file=sys.stderr)
        return 1

    print("[2/8] Finding a viral topic & angle...")
    picked = pick_topic(candidates, state.get("used_topics", []))
    print(f"      Chose: {picked['topic']}")

    print(f"[3/8] Writing high-retention script...")
    script = write_script(picked, target_minutes)
    
    narration_text = script.get("narration", f"The eerie truth behind {picked['topic']}.")
    word_count = len(narration_text.split())
    print(f"      {word_count} words ({word_count / 150:.1f} min)")
    print(f"      Final Video Title: {script.get('title', picked['topic'])}")

    run_dir = WORK / dt.datetime.now(dt.UTC).strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "script.txt").write_text(narration_text, encoding="utf-8")

    print("[4/8] Synthesizing ominous narration...")
    narration_mp3 = run_dir / "narration.mp3"
    word_srt = run_dir / "words.srt"
    synth_narration(narration_text, voice, narration_mp3, word_srt)
    cues = parse_srt(word_srt)
    if not cues:
        return 1
    print(f"      Audio ready: {cues[-1].end:.1f}s")

    print("[5/8] Directing B-Roll Segments...")
    segments = chunk_into_segments(cues, target_seconds=cfg.get("segment_seconds", 6.5))
    queries = queries_for_segments(segments)

    print("[6/8] Sourcing cinematic clips...")
    footage_dir = run_dir / "footage"
    footage_dir.mkdir(exist_ok=True)
    raw_clips = gather_footage(segments, queries, pexels_key, footage_dir)

    print("[7/8] Applying color grading, audio ducking & assembling...")
    norm_dir = run_dir / "normalized"
    norm_dir.mkdir(exist_ok=True)
    norm_clips: list[Path] = []
    for i, (clip, seg) in enumerate(zip(raw_clips, segments)):
        norm = norm_dir / f"n_{i:03d}.mp4"
        normalize_clip(clip, norm, seg.end - seg.start)
        norm_clips.append(norm)
    silent = run_dir / "silent.mp4"
    concat_normalized(norm_clips, silent)

    captions = run_dir / "captions.ass"
    build_caption_file(cues, captions)

    bgm = ROOT / "assets" / "bgm.mp3"
    final = run_dir / "final.mp4"
    mux_final(silent, narration_mp3, captions, bgm if bgm.exists() else None, final)

    print("[8/8] Uploading to YouTube...")
    yt = youtube_client()
    
    desc_text = script.get('description', f"A deep dive into {picked['topic']}")
    description = f"{desc_text}\n\nSources used in research: {picked['source_url']}\n"
    
    vid_id = upload_video(
        yt, final,
        title=script.get("title", picked["topic"])[:100],
        description=description,
        tags=script.get("tags", ["documentary", "mystery", "unexplained", "creepy"]),
    )
    print(f"      Live -> https://youtu.be/{vid_id}")

    used = state.get("used_topics", [])
    used.append(picked["topic"])
    state["used_topics"] = used[-200:]
    save_state(state)

    shutil.rmtree(run_dir, ignore_errors=True)
    print("[done]")
    return 0

if __name__ == "__main__":
    sys.exit(main())

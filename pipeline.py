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
# Flattened to a single line to prevent web-editor copy-paste bugs.
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
        seen

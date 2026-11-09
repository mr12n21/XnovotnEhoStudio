"""Stahování zvuků z freesound.org.

Funguje na libovolné stránce, kde freesound zobrazuje přehrávače zvuků:
  - tagy:      https://freesound.org/browse/tags/?f=tag:%22short%22
  - hledání:   https://freesound.org/search/?q=kick+techno
  - packy:     https://freesound.org/people/<user>/packs/<id>/
  - uživatel:  https://freesound.org/people/<user>/sounds/
Ze stránky se vyčtou data-* atributy přehrávače (id, název, autor, délka, odkaz
na náhled) a stáhne se HQ náhled (OGG ~ 192 kbit/s). Stránkuje se přes ?page=N.
"""
from __future__ import annotations

import html
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from . import config
from .library import load_meta, namespace_dir, save_meta, slug

PLAYER_RE = re.compile(r'<div\s+class="bw-player"(.*?)>', re.S)
ATTR_RE = re.compile(r'data-([a-z0-9-]+)="([^"]*)"')
LICENSE_RE = re.compile(r'title="License: ([^"]+)"')
SOUND_LINK_RE = re.compile(r'href="(/people/[^"/]+/sounds/(\d+)/)"')


@dataclass
class RemoteSound:
    id: str
    title: str
    username: str
    duration: float
    license: str
    url: str
    preview: str


def fetch(url: str, timeout: float = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": config.USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def parse_sounds(page: str) -> list[RemoteSound]:
    matches = list(PLAYER_RE.finditer(page))
    sounds: list[RemoteSound] = []
    seen = set()
    for i, m in enumerate(matches):
        attrs = {k: html.unescape(v) for k, v in ATTR_RE.findall(m.group(1))}
        sid = attrs.get("sound-id")
        preview = attrs.get("ogg") or attrs.get("mp3")
        if not sid or not preview or sid in seen:
            continue
        seen.add(sid)
        # zbytek HTML až k dalšímu přehrávači patří k tomuto zvuku
        end = matches[i + 1].start() if i + 1 < len(matches) else len(page)
        chunk = page[m.end():end]
        lic = LICENSE_RE.search(chunk)
        link = next((l for l, lid in SOUND_LINK_RE.findall(chunk) if lid == sid), None)
        user = attrs.get("username", "")
        sounds.append(RemoteSound(
            id=sid,
            title=attrs.get("title") or f"sound {sid}",
            username=user,
            duration=float(attrs.get("duration") or 0),
            license=html.unescape(lic.group(1)) if lic else "",
            url="https://freesound.org" + (link or f"/people/{user}/sounds/{sid}/"),
            # -lq je ~ 64 kbit/s, -hq ~ 192 kbit/s
            preview=preview.replace("-lq.", "-hq."),
        ))
    return sounds


def page_url(url: str, page: int) -> str:
    parts = urllib.parse.urlsplit(url)
    q = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
    if page > 1:
        q.append(("page", str(page)))
    return urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q), fragment=""))


def suggest_namespace(url: str) -> str:
    """Navrhne název kategorie z URL tabu."""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return ""
    q = dict(urllib.parse.parse_qsl(parts.query))
    tags = re.findall(r'tag:"?([^"\s]+)"?', q.get("f", ""))
    if tags:
        return "+".join(tags)
    if q.get("q"):
        return q["q"].replace(" ", "_")
    segs = [s for s in parts.path.split("/") if s]
    if "packs" in segs:
        rest = segs[segs.index("packs") + 1:]
        return f"pack_{rest[0]}" if rest else f"{segs[1]}_packs"
    if segs[:1] == ["people"] and len(segs) >= 2:
        return segs[1]
    return segs[-1] if segs else "freesound"


@dataclass
class DownloadStats:
    found: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0


def download(url: str, namespace: str, pages: int = 3, max_duration: float = 30.0,
             log: Callable[[str], None] = print,
             progress: Callable[[int, int], None] = lambda a, b: None,
             should_stop: Callable[[], bool] = lambda: False) -> DownloadStats:
    stats = DownloadStats()
    d = namespace_dir(namespace)
    meta = load_meta(namespace)
    known = {m.get("fs_id") or uid for uid, m in meta.items()}

    queue: list[RemoteSound] = []
    for p in range(1, max(1, pages) + 1):
        if should_stop():
            break
        purl = page_url(url, p)
        log(f"Stránka {p}: {purl}")
        try:
            text = fetch(purl).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            log(f"  HTTP {e.code} – konec stránkování")
            break
        except Exception as e:  # síť, timeout
            log(f"  chyba: {e}")
            break
        found = parse_sounds(text)
        log(f"  nalezeno {len(found)} zvuků")
        if not found:
            break
        queue.extend(found)
        if f"page={p + 1}" not in text:
            break
        time.sleep(config.REQUEST_DELAY)

    stats.found = len(queue)
    for i, s in enumerate(queue, 1):
        if should_stop():
            log("Zastaveno.")
            break
        progress(i, len(queue))
        if s.id in known:
            stats.skipped += 1
            continue
        if max_duration and s.duration > max_duration:
            log(f"  přeskočeno (dlouhé {s.duration:.0f}s): {s.title}")
            stats.skipped += 1
            continue
        ext = ".ogg" if s.preview.endswith(".ogg") else ".mp3"
        fname = f"{s.id}_{slug(s.title)}{ext}"
        try:
            data = fetch(s.preview, timeout=60)
            tmp = d / (fname + ".part")
            tmp.write_bytes(data)
            tmp.replace(d / fname)
        except Exception as e:
            log(f"  CHYBA {s.title}: {e}")
            stats.failed += 1
            continue
        meta[s.id] = {
            "fs_id": s.id, "title": s.title, "file": fname, "username": s.username,
            "license": s.license, "url": s.url, "duration": s.duration, "source": url,
        }
        known.add(s.id)
        stats.downloaded += 1
        log(f"  ↓ {s.title}  ({s.username}, {s.duration:.1f}s, {s.license or '?'})")
        if stats.downloaded % 5 == 0:
            save_meta(namespace, meta)
        time.sleep(0.05)
    save_meta(namespace, meta)
    log(f"Hotovo: staženo {stats.downloaded}, přeskočeno {stats.skipped}, chyb {stats.failed}")
    return stats

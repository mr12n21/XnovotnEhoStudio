"""Knihovna zvuků na disku.

Struktura:
    library/<kategorie>/sounds.json      metadata (autor, licence, URL, ...)
    library/<kategorie>/<id>_<nazev>.ogg  audio

Kategorie (namespace) = jedna složka = typicky jeden "tab" z freesound.org.
"""
from __future__ import annotations

import json
import re
import shutil
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config

AUDIO_EXT = {".ogg", ".mp3", ".wav", ".flac", ".aif", ".aiff", ".opus"}
META_FILE = "sounds.json"


@dataclass
class Sound:
    uid: str
    title: str
    file: str
    namespace: str
    username: str = ""
    license: str = ""
    url: str = ""
    duration: float = 0.0
    source: str = ""

    @property
    def key(self) -> str:
        return f"{self.namespace}/{self.uid}"

    @property
    def path(self) -> Path:
        return config.LIBRARY_DIR / self.namespace / self.file

    def meta(self) -> dict:
        d = asdict(self)
        d.pop("namespace")
        return d


def slug(text: str, maxlen: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_.")
    return (text[:maxlen] or "sound").strip("_")


def safe_namespace(name: str) -> str:
    name = re.sub(r"[/\\:*?\"<>|]+", "_", name.strip()).strip(". ")
    return name[:60] or "kategorie"


def namespace_dir(ns: str) -> Path:
    d = config.LIBRARY_DIR / ns
    d.mkdir(parents=True, exist_ok=True)
    return d


def load_meta(ns: str) -> dict[str, dict]:
    p = config.LIBRARY_DIR / ns / META_FILE
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_meta(ns: str, meta: dict[str, dict]) -> None:
    p = namespace_dir(ns) / META_FILE
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), "utf-8")
    tmp.replace(p)


def scan_namespace(ns: str) -> list[Sound]:
    d = config.LIBRARY_DIR / ns
    meta = load_meta(ns)
    sounds: list[Sound] = []
    known_files = set()
    for uid, m in meta.items():
        f = m.get("file", "")
        if not f or not (d / f).exists():
            continue
        known_files.add(f)
        sounds.append(Sound(
            uid=uid, title=m.get("title", f), file=f, namespace=ns,
            username=m.get("username", ""), license=m.get("license", ""),
            url=m.get("url", ""), duration=float(m.get("duration", 0) or 0),
            source=m.get("source", ""),
        ))
    # soubory nakopírované ručně do složky (bez metadat)
    for p in sorted(d.iterdir()):
        if p.suffix.lower() in AUDIO_EXT and p.name not in known_files:
            sounds.append(Sound(uid=f"file:{p.stem}", title=p.stem, file=p.name, namespace=ns))
    sounds.sort(key=lambda s: s.title.lower())
    return sounds


def scan_library() -> dict[str, list[Sound]]:
    config.LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    result = {}
    for d in sorted(config.LIBRARY_DIR.iterdir(), key=lambda p: p.name.lower()):
        if d.is_dir() and not d.name.startswith("."):
            result[d.name] = scan_namespace(d.name)
    return result


def import_files(paths: list[str], ns: str) -> int:
    """Zkopíruje lokální audio soubory do kategorie."""
    d = namespace_dir(ns)
    meta = load_meta(ns)
    n = 0
    for src in map(Path, paths):
        if src.suffix.lower() not in AUDIO_EXT:
            continue
        uid = f"local:{slug(src.stem, 60)}"
        dst = d / f"{slug(src.stem, 60)}{src.suffix.lower()}"
        if not dst.exists():
            shutil.copy2(src, dst)
        meta[uid] = {"title": src.stem, "file": dst.name, "source": str(src)}
        n += 1
    save_meta(ns, meta)
    return n


def copy_sound(sound: Sound, target_ns: str) -> bool:
    """Zkopíruje zvuk (soubor + metadata) do jiné kategorie – vlastní výběry typu 'kicks'."""
    if target_ns == sound.namespace:
        return False
    d = namespace_dir(target_ns)
    dst = d / sound.file
    if not dst.exists():
        shutil.copy2(sound.path, dst)
    meta = load_meta(target_ns)
    meta[sound.uid] = sound.meta()
    save_meta(target_ns, meta)
    return True


def remove_sound(sound: Sound) -> None:
    meta = load_meta(sound.namespace)
    meta.pop(sound.uid, None)
    save_meta(sound.namespace, meta)
    try:
        sound.path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------- audio

def resample(buf: np.ndarray, new_len: int) -> np.ndarray:
    n = len(buf)
    if n == new_len or n < 2:
        return buf
    x_old = np.arange(n, dtype=np.float64)
    x_new = np.linspace(0, n - 1, new_len)
    out = np.empty((new_len, buf.shape[1]), dtype=np.float32)
    for c in range(buf.shape[1]):
        out[:, c] = np.interp(x_new, x_old, buf[:, c])
    return out


def apply_edge_fades(buf: np.ndarray, sr: int, fade_in_ms=0.5, fade_out_ms=5.0) -> np.ndarray:
    n = len(buf)
    fi = min(n // 4, int(sr * fade_in_ms / 1000))
    fo = min(n // 4, int(sr * fade_out_ms / 1000))
    if fi > 0:
        buf[:fi] *= np.linspace(0, 1, fi, dtype=np.float32)[:, None]
    if fo > 0:
        buf[-fo:] *= np.linspace(1, 0, fo, dtype=np.float32)[:, None]
    return buf


def load_audio(path: Path, sr: int = config.SAMPLE_RATE,
               max_seconds: float = config.MAX_SOUND_SECONDS) -> np.ndarray:
    """Načte soubor jako float32 stereo (N, 2) na vzorkovací frekvenci enginu."""
    data, file_sr = sf.read(str(path), dtype="float32", always_2d=True)
    if data.shape[1] == 1:
        data = np.repeat(data, 2, axis=1)
    elif data.shape[1] > 2:
        data = data[:, :2]
    if file_sr != sr:
        data = resample(data, int(round(len(data) * sr / file_sr)))
    max_frames = int(max_seconds * sr)
    if len(data) > max_frames:
        data = data[:max_frames]
    if len(data) == 0:
        data = np.zeros((1, 2), np.float32)
    data = np.ascontiguousarray(data, dtype=np.float32)
    return apply_edge_fades(data, sr)

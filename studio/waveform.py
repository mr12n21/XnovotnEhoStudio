"""Náhledy vln (peaky) – počítají se jednou a ukládají do library/<kategorie>/peaks.json."""
from __future__ import annotations

import json

import numpy as np

from . import config
from .library import Sound

N_PEAKS = 160
PEAK_FILE = "peaks.json"


def compute_peaks(buf: np.ndarray, n: int = N_PEAKS) -> np.ndarray:
    """Maximum amplitudy v n úsecích, odmocninová škála (tiché části jsou líp vidět)."""
    mono = np.abs(buf).max(axis=1) if buf.ndim == 2 else np.abs(buf)
    if len(mono) < n:
        mono = np.pad(mono, (0, n - len(mono)))
    edges = np.linspace(0, len(mono), n + 1).astype(np.int64)[:-1]
    p = np.maximum.reduceat(mono, edges)
    return np.sqrt(np.clip(p, 0, 1)).astype(np.float32)


class PeakCache:
    def __init__(self):
        self.mem: dict[str, np.ndarray] = {}
        self._loaded_ns: set[str] = set()
        self._dirty_ns: set[str] = set()

    def _load_ns(self, ns: str):
        if ns in self._loaded_ns:
            return
        self._loaded_ns.add(ns)
        p = config.LIBRARY_DIR / ns / PEAK_FILE
        try:
            data = json.loads(p.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for uid, hexs in data.items():
            try:
                arr = np.frombuffer(bytes.fromhex(hexs), np.uint8).astype(np.float32) / 255
                self.mem.setdefault(f"{ns}/{uid}", arr)
            except ValueError:
                pass

    def get(self, sound: Sound) -> np.ndarray | None:
        self._load_ns(sound.namespace)
        return self.mem.get(sound.key)

    def put(self, sound: Sound, peaks: np.ndarray):
        self._load_ns(sound.namespace)
        self.mem[sound.key] = peaks
        self._dirty_ns.add(sound.namespace)

    def flush(self):
        for ns in self._dirty_ns:
            d = config.LIBRARY_DIR / ns
            if not d.is_dir():
                continue
            prefix = ns + "/"
            data = {k[len(prefix):]: (np.clip(v, 0, 1) * 255).astype(np.uint8).tobytes().hex()
                    for k, v in self.mem.items() if k.startswith(prefix)}
            tmp = d / (PEAK_FILE + ".tmp")
            tmp.write_text(json.dumps(data), "utf-8")
            tmp.replace(d / PEAK_FILE)
        self._dirty_ns.clear()

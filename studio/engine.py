"""Mixovací engine.

Každý zvuk = Track, který neustále běží ve smyčce. Pozice ve smyčce se počítá
z globální pozice transportu (frame % délka_smyčky), takže všechny stopy jsou
vždy srovnané na beat grid – zapnutí stopy ji jen "zesílí", nerozhodí rytmus.

Délka smyčky:
  auto  – zaokrouhlí se nahoru na 1/4, 1/2, 1, 2, 4 ... 64 dob (doplní tichem)
  free  – přirozená délka zvuku (polyrytmy, ambient)
  N     – pevně N dob (delší zvuk se ořízne, kratší doplní tichem)
  fit   – zvuk se přetáhne (resample) přesně na délku smyčky -> loopy s jiným
          tempem sedí do tempa (mění se výška tónu, jako na gramofonu)
"""
from __future__ import annotations

import math
import re
import queue
import threading
import traceback
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config
from .library import Sound, apply_edge_fades, resample

BEAT_CHOICES = [0.25, 0.5, 1, 2, 4, 8, 16, 32, 64]
LENGTH_OPTIONS = ["auto", "free"] + BEAT_CHOICES
BEATS_PER_BAR = 4
BPM_RE = re.compile(r"(?<!\d)(\d{2,3}(?:[.,]\d+)?)\s*-?\s*bpm", re.I)


def bpm_from_title(title: str) -> float | None:
    m = BPM_RE.search(title)
    if m:
        v = float(m.group(1).replace(",", "."))
        if 50 <= v <= 220:
            return v
    return None


def length_label(v) -> str:
    if v in ("auto", "free"):
        return v
    return {0.25: "1/4", 0.5: "1/2"}.get(v, str(int(v)))


class Track:
    def __init__(self, sound: Sound):
        self.sound = sound
        self.key = sound.key
        self.group = sound.namespace
        self.buffer: np.ndarray | None = None      # originální audio
        self.state: tuple[np.ndarray, int] | None = None  # (přehrávací buffer, délka smyčky)
        self.enabled = False
        self.want = False            # uživatel chce zapnout (čeká se na načtení)
        self.loading = False
        self.volume = 0.8
        self.pan = 0.0
        self.length = "auto"
        self.src_bpm = bpm_from_title(sound.title)   # tempo z názvu ("... 170bpm")
        self.fit = self.src_bpm is not None          # loopy s tempem v názvu rovnou do tempa
        self.gain = np.zeros(2, np.float32)   # aktuální zisk L/R (vyhlazování)
        self.pending: tuple[bool, int] | None = None  # (stav, frame) – kvantizované přepnutí

    @property
    def on(self) -> bool:
        """Stav z pohledu uživatele (včetně čekajících přepnutí)."""
        if self.pending is not None:
            return self.pending[0]
        return self.enabled or self.want


class Group:
    def __init__(self, name: str):
        self.name = name
        self.volume = 0.8
        self.mute = False
        self.solo = False


def soft_clip(x: np.ndarray, t: float = 0.85) -> np.ndarray:
    """Měkký limiter – pod prahem lineární, nad ním tanh koleno."""
    a = np.abs(x)
    over = a > t
    if over.any():
        x[over] = np.sign(x[over]) * (t + (1 - t) * np.tanh((a[over] - t) / (1 - t)))
    return x


class Engine:
    def __init__(self, sr: int = config.SAMPLE_RATE):
        self.sr = sr
        self.bpm = 130.0
        self.master = 0.8
        self.quantize = "beat"     # off | beat | bar
        self.playing = False
        self.frame = 0
        self.peak = 0.0
        self.tracks: dict[str, Track] = {}
        self.groups: dict[str, Group] = {}
        self._live: tuple[Track, ...] = ()   # stopy s načteným audiem
        self._groups_t: tuple[Group, ...] = ()
        self._rec: tuple[queue.Queue, threading.Thread, Path] | None = None
        self._rec_used: set[str] = set()
        self.last_error = ""

    # ------------------------------------------------------------ struktura
    def add_sound(self, sound: Sound) -> Track:
        t = self.tracks.get(sound.key)
        if t is None:
            t = Track(sound)
            self.tracks[sound.key] = t
        else:
            t.sound = sound
        self.group(sound.namespace)
        return t

    def remove_track(self, key: str):
        t = self.tracks.pop(key, None)
        if t:
            t.enabled = False
            self._live = tuple(x for x in self._live if x is not t)

    def group(self, name: str) -> Group:
        g = self.groups.get(name)
        if g is None:
            g = Group(name)
            self.groups[name] = g
            self._groups_t = tuple(self.groups.values())
        return g

    def set_buffer(self, t: Track, buf: np.ndarray):
        t.buffer = buf
        self.prepare(t)
        if t not in self._live:
            self._live = self._live + (t,)

    # ------------------------------------------------------------ tempo / délky
    @property
    def frames_per_beat(self) -> float:
        return self.sr * 60.0 / self.bpm

    def set_bpm(self, bpm: float):
        # zachovat pozici v taktu při změně tempa
        beat_pos = self.frame / self.frames_per_beat
        self.bpm = float(bpm)
        self.frame = int(beat_pos * self.frames_per_beat)
        for t in self._live:
            self.prepare(t)

    def loop_beats(self, t: Track) -> float | None:
        if t.buffer is None or t.length == "free":
            return None
        if t.length != "auto":
            return float(t.length)
        n = len(t.buffer)
        fpb = self.frames_per_beat
        if t.fit and t.src_bpm:
            return max(1.0, float(round(n / self.sr * t.src_bpm / 60)))
        if t.fit:
            ratio = max(n / fpb, 1e-6)
            return min(BEAT_CHOICES, key=lambda b: abs(math.log2(b) - math.log2(ratio)))
        # nahoru na nejbližší délku; ~8 % dozvuku na konci se smí useknout
        return next((b for b in BEAT_CHOICES if b * fpb >= n * 0.92), BEAT_CHOICES[-1])

    def prepare(self, t: Track):
        buf = t.buffer
        if buf is None:
            t.state = None
            return
        n = len(buf)
        beats = self.loop_beats(t)
        if beats is None:
            t.state = (buf, n)
            return
        L = max(1, int(round(beats * self.frames_per_beat)))
        if t.fit and n != L:
            play = resample(buf, L)
        elif n > L:
            play = apply_edge_fades(buf[:L].copy(), self.sr, 0, 4.0)
        else:
            play = buf
        t.state = (play, L)

    # ------------------------------------------------------------ ovládání
    def next_boundary(self) -> int:
        if self.quantize == "off" or not self.playing:
            return -1
        step = self.frames_per_beat * (BEATS_PER_BAR if self.quantize == "bar" else 1)
        return int(math.ceil(self.frame / step) * step)

    def request(self, t: Track, on: bool, at: int | None = None):
        """Zapne/vypne stopu (kvantizovaně podle nastavení)."""
        at = self.next_boundary() if at is None else at
        if at < 0:
            t.pending = None
            t.enabled = on
        else:
            t.pending = (on, at)

    def play(self):
        self.playing = True

    def stop(self):
        self.playing = False
        self.frame = 0
        for t in self._live:
            if t.pending is not None:
                t.enabled = t.pending[0]
                t.pending = None
            t.gain[:] = 0

    def all_off(self):
        for t in self.tracks.values():
            t.pending = None
            t.enabled = False
            t.want = False

    # ------------------------------------------------------------ mix
    def _target(self, t: Track, any_solo: bool) -> np.ndarray:
        g = self.groups.get(t.group)
        if g is None or g.mute or (any_solo and not g.solo) or not t.enabled:
            return np.zeros(2, np.float32)
        v = t.volume * g.volume
        return np.array([v * min(1.0, 1.0 - t.pan), v * min(1.0, 1.0 + t.pan)], np.float32)

    @staticmethod
    def _mix_track(out: np.ndarray, state, positions: np.ndarray, gains):
        play, L = state
        m = len(play)
        idx = positions % L
        if m >= L:
            s = play[idx]
        else:
            s = play[np.minimum(idx, m - 1)]
            s *= (idx < m)[:, None]
        out += s * gains

    def render(self, frames: int) -> np.ndarray:
        """Volá audio vlákno. Vrací (frames, 2) float32."""
        out = np.zeros((frames, 2), np.float32)
        if not self.playing:
            self.peak = 0.0
            return out
        try:
            start = self.frame
            end = start + frames
            positions = np.arange(start, end, dtype=np.int64)
            ramp = None
            any_solo = any(g.solo for g in self._groups_t)
            rec = self._rec
            for t in self._live:
                p = t.pending
                if p is not None and p[1] < end:
                    t.enabled = p[0]
                    t.pending = None
                st = t.state
                if st is None:
                    continue
                target = self._target(t, any_solo)
                g0 = t.gain
                if not g0.any() and not target.any():
                    continue
                if np.array_equal(g0, target):
                    gains = target
                else:
                    if ramp is None:
                        ramp = np.linspace(0.0, 1.0, frames, dtype=np.float32)[:, None]
                    gains = g0 + (target - g0) * ramp
                    t.gain = target
                self._mix_track(out, st, positions, gains)
                if rec is not None:
                    self._rec_used.add(t.key)
            out *= self.master
            soft_clip(out)
            self.peak = float(np.abs(out).max()) if frames else 0.0
            self.frame = end
            if rec is not None:
                rec[0].put(out.copy())
        except Exception:  # audio vlákno nesmí spadnout
            self.last_error = traceback.format_exc()
            out[:] = 0
        return out

    # ------------------------------------------------------------ nahrávání
    @property
    def recording(self) -> bool:
        return self._rec is not None

    def start_recording(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        q: queue.Queue = queue.Queue()
        f = sf.SoundFile(str(path), "w", self.sr, 2, subtype="PCM_24")

        def writer():
            with f:
                while (item := q.get()) is not None:
                    f.write(item)

        th = threading.Thread(target=writer, daemon=True)
        th.start()
        self._rec_used = set()
        self._rec = (q, th, path)

    def stop_recording(self) -> tuple[Path, set[str]] | None:
        rec = self._rec
        if rec is None:
            return None
        self._rec = None
        rec[0].put(None)
        rec[1].join(timeout=5)
        return rec[2], set(self._rec_used)

    # ------------------------------------------------------------ offline export
    def enabled_tracks(self) -> list[Track]:
        return [t for t in self.tracks.values() if t.on and t.state is not None]

    def render_offline(self, path: Path, frames_total: int, tracks: list[Track],
                       respect_groups: bool = True, limiter: bool = True,
                       master: float | None = None, chunk: int = 65536):
        """Vyrenderuje stopy (v jejich aktuálním nastavení) od začátku taktu 1."""
        path.parent.mkdir(parents=True, exist_ok=True)
        any_solo = any(g.solo for g in self._groups_t)
        gains = {}
        for t in tracks:
            if respect_groups:
                was = t.enabled
                t.enabled = True
                gains[t.key] = self._target(t, any_solo)
                t.enabled = was
            else:
                v = t.volume
                gains[t.key] = np.array([v * min(1, 1 - t.pan), v * min(1, 1 + t.pan)], np.float32)
        m = self.master if master is None else master
        with sf.SoundFile(str(path), "w", self.sr, 2, subtype="PCM_24") as f:
            for start in range(0, frames_total, chunk):
                n = min(chunk, frames_total - start)
                out = np.zeros((n, 2), np.float32)
                pos = np.arange(start, start + n, dtype=np.int64)
                for t in tracks:
                    if t.state is not None and gains[t.key].any():
                        self._mix_track(out, t.state, pos, gains[t.key])
                out *= m
                if limiter:
                    soft_clip(out)
                np.clip(out, -1, 1, out=out)
                f.write(out)

    def frames_for_bars(self, bars: int) -> int:
        return int(round(bars * BEATS_PER_BAR * self.frames_per_beat))

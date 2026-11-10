"""Mixovací engine.

Pojmy:
  Deck   – samostatný mixer (vlastní okno). Má vlastní výběr zapnutých zvuků,
           hlasitost a start/stop. Všechny decky sdílí jedno tempo.
  Track  – jeden zvuk v jednom decku; běží v nekonečné smyčce.
  Group  – kategorie (namespace) uvnitř decku – hlasitost / mute / solo.
           Mute/solo se mění kvantizovaně (na takt), aby šly použít na break/drop.

Minimalistika: Engine.max_tracks (kolik zvuků smí hrát najednou) a Group.one
(jen jeden zvuk z kategorie). Při překročení se vypnou nejstarší (Track.seq).

Čas: globální pozice `frame` (vzorky od Play). Pozice ve smyčce se počítá
relativně ke startu decku: (frame - deck.start) % délka_smyčky – takže když deck
naskočí, všechny jeho smyčky začínají od začátku (důležité pro drop).

Propojení decků: deck A má "spouštěč" = svůj zvuk + počet opakování. Po N
dohráních toho zvuku se A přesně na vzorek vypne a spustí se deck B.

Render se dělí v místech událostí (start/stop decku, kvantizované přepnutí),
takže všechno sedí na vzorek přesně bez ohledu na velikost audio bufferu.
"""
from __future__ import annotations

import math
import queue
import re
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
RAMP = 96          # vzorků – plynulé zapnutí/vypnutí (bez lupnutí)
TAIL_FADE = 256    # vzorků – doznění při zastavení decku
ZERO2 = np.zeros(2, np.float32)

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


def soft_clip(x: np.ndarray, t: float = 0.85) -> np.ndarray:
    """Měkký limiter – pod prahem lineární, nad ním tanh koleno."""
    a = np.abs(x)
    over = a > t
    if over.any():
        x[over] = np.sign(x[over]) * (t + (1 - t) * np.tanh((a[over] - t) / (1 - t)))
    return x


def pan_gains(v: float, pan: float) -> np.ndarray:
    return np.array([v * min(1.0, 1.0 - pan), v * min(1.0, 1.0 + pan)], np.float32)


def loop_beats(n: int, sr: int, bpm: float, length, fit: bool, src_bpm: float | None) -> float | None:
    """Délka smyčky v dobách (None = přirozená délka)."""
    if length == "free":
        return None
    if length != "auto":
        return float(length)
    fpb = sr * 60.0 / bpm
    if fit and src_bpm:
        return max(1.0, float(round(n / sr * src_bpm / 60)))
    if fit:
        ratio = max(n / fpb, 1e-6)
        return min(BEAT_CHOICES, key=lambda b: abs(math.log2(b) - math.log2(ratio)))
    # nahoru na nejbližší délku; ~8 % dozvuku na konci se smí useknout
    return next((b for b in BEAT_CHOICES if b * fpb >= n * 0.92), BEAT_CHOICES[-1])


def prepare_state(buf: np.ndarray, sr: int, bpm: float, length, fit: bool, src_bpm):
    """Vrátí (přehrávací buffer, délka smyčky ve vzorcích)."""
    n = len(buf)
    beats = loop_beats(n, sr, bpm, length, fit, src_bpm)
    if beats is None:
        return buf, n
    L = max(1, int(round(beats * sr * 60.0 / bpm)))
    if fit and n != L:
        play = resample(buf, L)
    elif n > L:
        play = apply_edge_fades(buf[:L].copy(), sr, 0, 4.0)
    else:
        play = buf
    return play, L


def mix_loop(out: np.ndarray, state, positions: np.ndarray, gains):
    play, L = state
    m = len(play)
    idx = positions % L
    if m >= L:
        s = play[idx]
    else:
        s = play[np.minimum(idx, m - 1)]
        s *= (idx < m)[:, None]
    out += s * gains


class Track:
    def __init__(self, deck: "Deck", sound: Sound):
        self.deck = deck
        self.sound = sound
        self.key = sound.key
        self.group = sound.namespace
        self.state: tuple[np.ndarray, int] | None = None
        self.enabled = False
        self.want = False            # chce se zapnout, čeká na načtení audia
        self.volume = 0.8
        self.pan = 0.0
        self.length = "auto"
        self.src_bpm = bpm_from_title(sound.title)
        self.fit = self.src_bpm is not None
        self.gain = np.zeros(2, np.float32)
        self.jump = False            # další blok bez náběhu (start decku na začátku smyčky)
        self.pending: tuple[bool, int] | None = None
        self.seq = 0                 # pořadí zapnutí (limit vypíná nejstarší)

    @property
    def on(self) -> bool:
        if self.pending is not None:
            return self.pending[0]
        return self.enabled or self.want

    def copy_settings(self, o: "Track"):
        self.volume, self.pan, self.length, self.fit = o.volume, o.pan, o.length, o.fit


class Group:
    def __init__(self, name: str):
        self.name = name
        self.volume = 0.8
        self.mute = False
        self.solo = False
        self.one = False             # jen jeden zvuk z kategorie najednou
        self.pending: dict[str, tuple[bool, int]] = {}   # mute/solo naplánované na takt

    def target(self, attr: str) -> bool:
        """Hodnota mute/solo po naplánované změně."""
        p = self.pending.get(attr)
        return p[0] if p is not None else getattr(self, attr)


class Deck:
    def __init__(self, id_: int, name: str):
        self.id = id_
        self.name = name
        self.tracks: dict[str, Track] = {}
        self.groups: dict[str, Group] = {}
        self._live: tuple[Track, ...] = ()
        self._groups_t: tuple[Group, ...] = ()
        self.volume = 1.0
        self.mute = False
        self.exclusive = False        # při startu zastavit ostatní decky
        self.start: int | None = None
        self.stop: int | None = None
        self.was_running = False
        self.columns: list[str] = []  # kategorie otevřené v okně decku
        # spouštěč: po N dohráních zvuku link_key spustit deck link_target
        self.link_key: str | None = None
        self.link_repeats = 1
        self.link_target: int | None = None
        self._sched_start: int | None = None
        self._linked_stop: int | None = None
        self._link_target_start: int | None = None
        self._link_sched_target: int | None = None

    def group(self, name: str) -> Group:
        g = self.groups.get(name)
        if g is None:
            g = Group(name)
            self.groups[name] = g
            self._groups_t = tuple(self.groups.values())
        return g

    def running_at(self, f: int) -> bool:
        return self.start is not None and self.start <= f and (self.stop is None or f < self.stop)

    def status(self, f: int) -> str:
        if self.start is None:
            return "stopped"
        if self.start > f:
            return "waiting"
        if self.stop is not None:
            return "stopping"
        return "playing"


class Engine:
    def __init__(self, sr: int = config.SAMPLE_RATE):
        self.sr = sr
        self.bpm = 130.0
        self.master = 0.8
        self.quantize = "bar"     # off | beat | bar
        self.max_tracks = 0       # kolik zvuků smí hrát najednou (0 = bez limitu)
        self._seq = 0
        self.playing = False
        self.frame = 0
        self.peak = 0.0
        self.lock = threading.RLock()
        self.buffers: dict[str, np.ndarray] = {}
        self.decks: dict[int, Deck] = {}
        self._decks_t: tuple[Deck, ...] = ()
        self._next_id = 1
        self._rec: tuple[queue.Queue, threading.Thread, Path] | None = None
        self._rec_used: set[str] = set()
        self.recorder = None       # arrangement.Recorder – záznam událostí pro postprodukci
        self.arr_player = None     # arrangement.ArrPlayer – přehrávání postprodukce
        self.last_error = ""

    @property
    def fpb(self) -> float:
        return self.sr * 60.0 / self.bpm

    # ------------------------------------------------------------ decky
    def _decks_changed(self):
        self._decks_t = tuple(self.decks.values())

    def new_deck(self, name: str | None = None, id_: int | None = None) -> Deck:
        with self.lock:
            if id_ is None:
                id_ = self._next_id
            self._next_id = max(self._next_id, id_ + 1)
            d = Deck(id_, name or f"Deck {id_}")
            self.decks[d.id] = d
            self._decks_changed()
            return d

    def remove_deck(self, d: Deck):
        with self.lock:
            self._cancel_link(d)
            self.decks.pop(d.id, None)
            self._decks_changed()
            for o in self._decks_t:
                if o.link_target == d.id:
                    o.link_target = None
                    if o.start is not None:
                        self._schedule_link(o)

    def duplicate_deck(self, d: Deck) -> Deck:
        n = self.new_deck(f"{d.name} kopie")
        with self.lock:
            n.volume, n.mute, n.exclusive = d.volume, d.mute, d.exclusive
            n.link_key, n.link_repeats = d.link_key, d.link_repeats
            n.columns = list(d.columns)
            for name, g in d.groups.items():
                ng = n.group(name)
                ng.volume, ng.mute, ng.solo = g.volume, g.mute, g.solo
            for t in d.tracks.values():
                nt = self.track(n, t.sound)
                nt.copy_settings(t)
                self.prepare(nt)
                if t.on:
                    if nt.state is not None:
                        nt.enabled = True
                    else:
                        nt.want = True
        return n

    def track(self, deck: Deck, sound: Sound) -> Track:
        t = deck.tracks.get(sound.key)
        if t is not None:
            t.sound = sound
            return t
        with self.lock:
            t = Track(deck, sound)
            deck.tracks[sound.key] = t
            deck.group(sound.namespace)
            if sound.key in self.buffers:
                self.prepare(t)
                deck._live = deck._live + (t,)
        return t

    def drop_sound(self, key: str):
        with self.lock:
            self.buffers.pop(key, None)
            for d in self._decks_t:
                t = d.tracks.pop(key, None)
                if t is not None:
                    d._live = tuple(x for x in d._live if x is not t)

    def set_buffer(self, key: str, buf: np.ndarray):
        with self.lock:
            self.buffers[key] = buf
            for d in self._decks_t:
                t = d.tracks.get(key)
                if t is None:
                    continue
                self.prepare(t)
                if t not in d._live:
                    d._live = d._live + (t,)
                if d.link_key == key and d.start is not None:
                    self._schedule_link(d)

    def prepare(self, t: Track):
        b = self.buffers.get(t.key)
        t.state = None if b is None else prepare_state(b, self.sr, self.bpm, t.length, t.fit, t.src_bpm)

    def set_track_params(self, t: Track, **kw):
        with self.lock:
            for k, v in kw.items():
                setattr(t, k, v)
            self.prepare(t)
            d = t.deck
            if d.link_key == t.key and d.start is not None:
                self._schedule_link(d)

    def set_bpm(self, bpm: float):
        with self.lock:
            r = self.bpm / float(bpm)

            def sc(f):
                return None if f is None else int(round(f * r))

            self.frame = sc(self.frame)
            for d in self._decks_t:
                d.start, d.stop = sc(d.start), sc(d.stop)
                d._sched_start, d._linked_stop = sc(d._sched_start), sc(d._linked_stop)
                d._link_target_start = sc(d._link_target_start)
                for t in d.tracks.values():
                    if t.pending is not None:
                        t.pending = (t.pending[0], sc(t.pending[1]))
                for g in d._groups_t:
                    g.pending = {k: (v, sc(at)) for k, (v, at) in g.pending.items()}
            self.bpm = float(bpm)
            for d in self._decks_t:
                for t in d._live:
                    self.prepare(t)
            for d in self._decks_t:
                if d.start is not None and d.link_target is not None:
                    self._schedule_link(d)

    # ------------------------------------------------------------ časová mřížka
    def _step(self) -> float:
        return self.fpb * (BEATS_PER_BAR if self.quantize == "bar" else 1)

    def global_boundary(self) -> int:
        if not self.playing or self.quantize == "off":
            return self.frame
        st = self._step()
        return int(math.ceil(self.frame / st) * st)

    def deck_boundary(self, d: Deck) -> int:
        if self.quantize == "off" or not self.playing or not d.running_at(self.frame):
            return -1
        st = self._step()
        return d.start + int(math.ceil((self.frame - d.start) / st) * st)

    # ------------------------------------------------------------ ovládání
    def request(self, t: Track, on: bool, at: int | None = None):
        """Zapne/vypne zvuk v decku (kvantizovaně podle nastavení)."""
        with self.lock:
            if at is None:
                at = self.deck_boundary(t.deck)
            if at < 0 or at <= self.frame:
                t.pending = None
                t.enabled = on
            else:
                t.pending = (on, at)

    def set_group(self, d: Deck, g: Group, attr: str, value: bool):
        """Mute/solo kategorie – kvantizovaně jako zapínání zvuků."""
        with self.lock:
            if value == getattr(g, attr):
                g.pending.pop(attr, None)
                return
            at = self.deck_boundary(d)
            if at < 0 or at <= self.frame:
                g.pending.pop(attr, None)
                setattr(g, attr, value)
            else:
                g.pending[attr] = (value, at)

    def mark_on(self, t: Track):
        """Zaznamená pořadí zapnutí (pro limit)."""
        self._seq += 1
        t.seq = self._seq

    def over_limit(self, d: Deck) -> list[Track]:
        """Zapnuté zvuky nad limit (max. počet, 1× v kategorii) – vždy ty nejstarší."""
        on = sorted((t for t in d.tracks.values() if t.on), key=lambda t: t.seq, reverse=True)
        keep, seen, out = 0, set(), []
        for t in on:
            g = d.groups.get(t.group)
            if (g is not None and g.one and t.group in seen) or (self.max_tracks and keep >= self.max_tracks):
                out.append(t)
            else:
                keep += 1
                seen.add(t.group)
        return out

    def start_deck(self, d: Deck):
        with self.lock:
            if self.arr_player is not None:
                self.arr_player.playing = False
            if not self.playing:
                self.playing = True
                at = self.frame
            else:
                st = d.status(self.frame)
                if st == "playing":
                    return
                if st == "stopping":       # zrušit naplánovaný konec
                    d.stop = None
                    d._linked_stop = None
                    self._cancel_link(d)
                    return
                at = self.global_boundary()
            d.start, d.stop, d._sched_start = at, None, None
            for t in d.tracks.values():
                if t.pending is not None:
                    t.enabled, t.pending = t.pending[0], None
            if d.exclusive:
                for o in self._decks_t:
                    if o is d or o.start is None:
                        continue
                    if o.start >= at:
                        o.start = None
                    elif o.stop is None or o.stop > at:
                        o.stop = at
                        self._cancel_link(o)

    def stop_deck(self, d: Deck):
        with self.lock:
            if d.start is None:
                return
            self._cancel_link(d)
            if d.start > self.frame:      # ještě nezačal
                d.start = None
                return
            at = self.deck_boundary(d)
            d.stop = self.frame if at < 0 else at
            d._linked_stop = None

    def reschedule(self, d: Deck):
        with self.lock:
            if d.start is not None:
                self._schedule_link(d)

    def _cancel_link(self, d: Deck):
        if d._link_target_start is not None:
            tgt = self.decks.get(d._link_sched_target)
            if tgt is not None and tgt.start == d._link_target_start and tgt.start > self.frame:
                tgt.start = None
            d._link_target_start = None

    def _schedule_link(self, d: Deck):
        """Naplánuje konec decku a start navázaného decku podle spouštěcího zvuku."""
        self._cancel_link(d)
        if d._linked_stop is not None and d.stop == d._linked_stop:
            d.stop = None
        d._linked_stop = None
        d._sched_start = d.start
        tgt = self.decks.get(d.link_target) if d.link_target is not None else None
        if d.start is None or tgt is None or tgt is d:
            return
        t = d.tracks.get(d.link_key) if d.link_key else None
        if t is None or t.state is None:
            return
        period = t.state[1] * max(1, int(d.link_repeats))
        k = max(1, math.ceil((self.frame - d.start + 1) / period))
        T = d.start + k * period
        d.stop = d._linked_stop = T
        tgt.start, tgt.stop, tgt._sched_start = T, None, None
        d._link_target_start, d._link_sched_target = T, tgt.id

    def link_eta(self, d: Deck) -> float | None:
        """Za kolik dob se spustí navázaný deck (None = nic naplánováno)."""
        if d._link_target_start is None:
            return None
        return max(0.0, (d._link_target_start - self.frame) / self.fpb)

    def stop_all(self):
        with self.lock:
            any_running = any(d.running_at(self.frame) for d in self._decks_t)
            for d in self._decks_t:
                if any_running:
                    d.was_running = d.running_at(self.frame)
                d.start = d.stop = d._sched_start = d._linked_stop = None
                d._link_target_start = None
                for t in d.tracks.values():
                    if t.pending is not None:
                        t.enabled, t.pending = t.pending[0], None
                    t.gain = ZERO2.copy()
                for g in d._groups_t:
                    for attr, (v, _) in g.pending.items():
                        setattr(g, attr, v)
                    g.pending.clear()
            self.playing = False
            self.frame = 0

    def all_off(self, deck: Deck | None = None):
        with self.lock:
            for d in ([deck] if deck else self._decks_t):
                for t in d.tracks.values():
                    t.pending = None
                    t.enabled = False
                    t.want = False

    # ------------------------------------------------------------ mix
    @staticmethod
    def track_gain(t: Track, d: Deck, any_solo: bool) -> float:
        g = d.groups.get(t.group)
        if not t.enabled or d.mute or g is None or g.mute or (any_solo and not g.solo):
            return 0.0
        return t.volume * g.volume * d.volume

    def _apply_events(self, f: int):
        for d in self._decks_t:
            for g in d._groups_t:
                if g.pending:
                    for attr, (v, at) in list(g.pending.items()):
                        if at <= f:
                            setattr(g, attr, v)
                            del g.pending[attr]
            if d.stop is not None and d.stop <= f:
                d.start = d.stop = d._sched_start = d._linked_stop = None
                for t in d._live:
                    t.gain = ZERO2.copy()
            if d.start is not None and d.start <= f and d._sched_start != d.start:
                self._schedule_link(d)
                for t in d._live:
                    t.jump = True
            for t in d._live:
                p = t.pending
                if p is not None and p[1] <= f:
                    t.enabled, t.pending = p[0], None
                    if t.enabled and t.state is not None and d.running_at(f):
                        ph = (f - d.start) % t.state[1]
                        if ph <= 1 or ph >= t.state[1] - 1:
                            t.jump = True
        if self.recorder is not None:
            self.recorder.update(self, f)

    def _mix_segment(self, out: np.ndarray, a: int, c: int):
        n = c - a
        for d in self._decks_t:
            if not d.running_at(a):
                continue
            any_solo = any(g.solo for g in d._groups_t)
            pos = np.arange(a - d.start, c - d.start, dtype=np.int64)
            tail = d.stop is not None and d.stop == c
            buf = np.zeros_like(out) if tail else out
            rec = self._rec is not None
            for t in d._live:
                st = t.state
                if st is None:
                    continue
                target = pan_gains(self.track_gain(t, d, any_solo), t.pan)
                if t.jump:
                    t.jump = False
                    t.gain = target
                g0 = t.gain
                if not g0.any() and not target.any():
                    continue
                if np.array_equal(g0, target):
                    gains = target
                else:
                    r = min(RAMP, n)
                    gains = np.empty((n, 2), np.float32)
                    gains[:] = target
                    frac = (np.arange(1, r + 1, dtype=np.float32) / RAMP)[:, None]
                    gains[:r] = g0 + (target - g0) * frac
                    t.gain = target if r >= RAMP else (g0 + (target - g0) * (r / RAMP)).astype(np.float32)
                mix_loop(buf, st, pos, gains)
                if rec:
                    self._rec_used.add(t.key)
            if tail:
                k = min(TAIL_FADE, n)
                buf[n - k:] *= np.linspace(1, 0, k, dtype=np.float32)[:, None]
                out += buf

    def _render_live(self, out: np.ndarray):
        s = self.frame
        e = s + len(out)
        cuts = set()
        for d in self._decks_t:
            for f in (d.start, d.stop):
                if f is not None and s < f < e:
                    cuts.add(f)
            for t in d._live:
                p = t.pending
                if p is not None and s < p[1] < e:
                    cuts.add(p[1])
            for g in d._groups_t:
                for _, at in g.pending.values():
                    if s < at < e:
                        cuts.add(at)
        a = s
        for c in sorted(cuts) + [e]:
            self._apply_events(a)
            if c > a:
                self._mix_segment(out[a - s:c - s], a, c)
            a = c
        self.frame = e

    def render(self, frames: int) -> np.ndarray:
        """Volá audio vlákno. Vrací (frames, 2) float32."""
        out = np.zeros((frames, 2), np.float32)
        try:
            with self.lock:
                ap = self.arr_player
                if ap is not None and ap.playing:
                    ap.render(out)
                elif self.playing:
                    self._render_live(out)
                else:
                    self.peak = 0.0
                    return out
                out *= self.master
                soft_clip(out)
                self.peak = float(np.abs(out).max()) if frames else 0.0
                rec = self._rec
                if rec is not None and self.playing:
                    rec[0].put(out.copy())
        except Exception:  # audio vlákno nesmí spadnout
            self.last_error = traceback.format_exc()
            out[:] = 0
        return out

    # ------------------------------------------------------------ nahrávání
    @property
    def recording(self) -> bool:
        return self._rec is not None

    def start_recording(self, path: Path, recorder=None):
        path.parent.mkdir(parents=True, exist_ok=True)
        q: queue.Queue = queue.Queue()
        f = sf.SoundFile(str(path), "w", self.sr, 2, subtype="PCM_24")

        def writer():
            with f:
                while (item := q.get()) is not None:
                    f.write(item)

        th = threading.Thread(target=writer, daemon=True)
        th.start()
        with self.lock:
            self._rec_used = set()
            self._rec = (q, th, path)
            self.recorder = recorder

    def stop_recording(self):
        """Vrátí (cesta k WAV, použité klíče, recorder)."""
        with self.lock:
            rec, recorder = self._rec, self.recorder
            self._rec = None
            self.recorder = None
        if rec is None:
            return None
        rec[0].put(None)
        rec[1].join(timeout=5)
        return rec[2], set(self._rec_used), recorder

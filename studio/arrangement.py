"""Postprodukce – nahrávka jako časová osa.

Při nahrávání se neukládá jen zvuk, ale i KDY který zvuk hrál:
  Lane   = jeden zvuk z jednoho decku (stopa na časové ose)
  Region = úsek, kdy stopa hrála (start/end v dobách, hlasitost, fáze smyčky)

Všechno je v dobách (beats), takže tempo jde změnit i zpětně. Úseky jde
posouvat, zkracovat, mazat a přidávat – výsledek se znovu spočítá ze zdrojových
zvuků (nic se neničí).

Region.phase: kde (relativně k začátku úseku) začíná smyčka. Při posunu úseku
se posouvá s ním; při oříznutí zleva se upraví, aby zvuk zůstal na místě.

Křivka hlasitosti (Lane.env, Arrangement.master_env): body [doba, hlasitost]
(1.0 = 100 %), mezi body se hlasitost plynule mění. Prázdná = všude 100 %.
Je navázaná na čas (ne na úseky) – hodí se na nájezdy, vyjetí a ztišení části.

Křivky úprav (Lane.tame_env / gate_env, Arrangement.tame_env / gate_env):
body [doba, síla 0–1], prázdná = nic se nedělá. Kreslí se stejně jako hlasitost.
  Špičky (tame) – místa výrazně hlasitější než běžná hlasitost stopy/mixu se
                  ztlumí (kompresor s předstihem).
  Šum (gate)    – tiché části pod úrovní zvuku (šum, brum, dozvuky mezi
                  ranami) se ztlumí (noise gate s předstihem).
Běžná hlasitost = medián špiček po dobách. Z toho vznikne křivka zisku
(CompiledLane.fx, Compiled.fx) – počítá se v pozadí jednou pro celou nahrávku,
takže přehrávání i export dají přesně stejný výsledek.
"""
from __future__ import annotations

import copy
import json
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import numpy as np
import soundfile as sf

from . import config
from .engine import (BEATS_PER_BAR, LENGTH_OPTIONS, bpm_from_title, mix_loop, pan_gains,
                     prepare_state, soft_clip)
from .library import Sound

FADE_IN = 44      # ~1 ms
FADE_OUT = 176    # ~4 ms
ENV_MAX = 2.0     # křivka hlasitosti max. 200 %
ENV_EPS = 1 / 32  # doby – krátký přechod na hraně výběru (skok hlasitosti necvakne)

FX_ATTRS = ("tame_env", "gate_env")   # křivky úprav (síla 0–1, prázdná = nic)
FX_BLOCK = 512             # vzorků – rozlišení výpočtu úprav (~12 ms)
TAME_RELEASE_DB_S = 30.0   # jak rychle se hlasitost po špičce vrací
GATE_FALL_DB_S = 300.0     # jak rychle se šum po zvuku ztlumí (~100 ms, plynulé doznění)


# =================================================================== křivka hlasitosti

def env_value(pts: list, beat: float) -> float:
    if not pts:
        return 1.0
    return float(np.interp(beat, [p[0] for p in pts], [p[1] for p in pts]))


def env_simplify(pts: list, neutral: float = 1.0, vmax: float = ENV_MAX) -> list:
    """Seřadí body, vyhodí zbytečné (uprostřed rovného úseku); samá neutrální hodnota -> prázdná křivka."""
    pts = sorted([float(b), min(vmax, max(0.0, float(v)))] for b, v in pts)
    out = []
    for i, p in enumerate(pts):
        if out and abs(out[-1][0] - p[0]) < 1e-9 and abs(out[-1][1] - p[1]) < 1e-6:
            continue
        if 0 < i < len(pts) - 1 and abs(pts[i - 1][1] - p[1]) < 1e-6 and abs(pts[i + 1][1] - p[1]) < 1e-6:
            continue
        out.append(p)
    if all(abs(v - neutral) < 1e-6 for _, v in out):
        return []
    return out


def env_cut(pts: list, a: float, b: float, neutral: float = 1.0, vmax: float = ENV_MAX) -> list:
    """Vyjmutí času [a, b) – body za výběrem se posunou doleva."""
    if not pts:
        return pts
    va, vb = env_value(pts, a), env_value(pts, b)
    d = b - a
    out = [list(p) for p in pts if p[0] < a] + [[a, va], [a + ENV_EPS, vb]]
    out += [[p[0] - d, p[1]] for p in pts if p[0] > b + ENV_EPS]
    return env_simplify(out, neutral, vmax)


def env_insert(pts: list, at: float, beats: float, neutral: float = 1.0, vmax: float = ENV_MAX) -> list:
    """Vložení prázdného času – body za ním se posunou doprava."""
    if not pts:
        return pts
    v = env_value(pts, at)
    out = [list(p) for p in pts if p[0] < at] + [[at, v], [at + beats, v]]
    out += [[p[0] + beats, p[1]] for p in pts if p[0] > at]
    return env_simplify(out, neutral, vmax)


def env_frames(pts: list, fpb: float):
    """Křivka v dobách -> (vzorky, hodnoty) pro render; None = beze změny."""
    if not pts:
        return None
    return (np.array([p[0] for p in pts], np.float64) * fpb, np.array([p[1] for p in pts], np.float64))


@dataclass
class Region:
    start: float
    end: float
    gain: float = 1.0
    phase: float = 0.0
    fade_in: float = 0.0     # nájezd v dobách
    fade_out: float = 0.0    # doznění v dobách

    @property
    def origin(self) -> float:
        return self.start + self.phase


@dataclass
class Lane:
    id: str
    key: str
    title: str
    ns: str
    file: str
    deck: str = ""
    username: str = ""
    license: str = ""
    url: str = ""
    length: object = "auto"
    fit: bool = False
    pan: float = 0.0
    volume: float = 1.0
    mute: bool = False
    solo: bool = False
    song: bool = False       # vlastní zvuk/písnička – hraje celá (neořezává se na 2 min)
    regions: list[Region] = field(default_factory=list)
    env: list = field(default_factory=list)   # křivka hlasitosti [[doba, hlasitost], ...]
    tame_env: list = field(default_factory=list)   # ztlumení špiček [[doba, síla 0–1], ...]
    gate_env: list = field(default_factory=list)   # odfiltrování šumu [[doba, síla 0–1], ...]

    @property
    def path(self) -> Path:
        return config.LIBRARY_DIR / self.ns / self.file

    def sound(self) -> Sound:
        return Sound(uid=self.key.split("/", 1)[-1], title=self.title, file=self.file, namespace=self.ns,
                     username=self.username, license=self.license, url=self.url)

    def sort(self):
        self.regions.sort(key=lambda r: r.start)


@dataclass
class Arrangement:
    bpm: float = 130.0
    lanes: list[Lane] = field(default_factory=list)
    audio: str = ""     # původní nahrávka (WAV) – jen pro referenci
    name: str = ""
    master_env: list = field(default_factory=list)   # křivka hlasitosti celého mixu
    tame_env: list = field(default_factory=list)     # ztlumení špiček celého mixu
    gate_env: list = field(default_factory=list)     # odfiltrování šumu celého mixu

    # --------------------------------------------------------------- I/O
    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "Arrangement":
        lanes = []
        for ld in d.get("lanes", []):
            ld = dict(ld)
            regs = [Region(**r) for r in ld.pop("regions", [])]
            for k in ("env",) + FX_ATTRS:
                ld[k] = sorted([float(b), float(v)] for b, v in ld.get(k, []))
            if ld.get("length") not in LENGTH_OPTIONS:
                ld["length"] = "auto"
            lanes.append(Lane(**ld, regions=regs))
        return Arrangement(bpm=float(d.get("bpm", 130)), lanes=lanes,
                           audio=d.get("audio", ""), name=d.get("name", ""),
                           master_env=sorted([float(b), float(v)] for b, v in d.get("master_env", [])),
                           tame_env=sorted([float(b), float(v)] for b, v in d.get("tame_env", []))
                           or ([[0.0, min(3, int(d["tame"])) / 3]] if d.get("tame") else []),  # starší verze
                           gate_env=sorted([float(b), float(v)] for b, v in d.get("gate_env", [])))

    def save(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(path)

    @staticmethod
    def load(path: Path) -> "Arrangement":
        return Arrangement.from_dict(json.loads(Path(path).read_text("utf-8")))

    def clone(self) -> "Arrangement":
        return copy.deepcopy(self)

    def end_beat(self) -> float:
        return max((r.end for l in self.lanes for r in l.regions), default=0.0)

    # --------------------------------------------------------------- úpravy času
    @staticmethod
    def _cut(r: Region, a: float, b: float) -> list[Region]:
        """Odstraní z úseku interval [a, b)."""
        if r.end <= a or r.start >= b:
            return [r]
        out = []
        if r.start < a:
            out.append(Region(r.start, a, r.gain, r.phase, fade_in=r.fade_in))
        if r.end > b:
            out.append(Region(b, r.end, r.gain, r.origin - b, fade_out=r.fade_out))
        return out

    def clear_range(self, a: float, b: float):
        """Ztiší výběr (čas zůstane)."""
        for l in self.lanes:
            l.regions = [p for r in l.regions for p in self._cut(r, a, b)]

    def delete_time(self, a: float, b: float):
        """Vyjme čas – všechno za výběrem se posune doleva."""
        self.clear_range(a, b)
        d = b - a
        for l in self.lanes:
            l.env = env_cut(l.env, a, b)
            for k in FX_ATTRS:
                setattr(l, k, env_cut(getattr(l, k), a, b, 0.0, 1.0))
            for r in l.regions:
                if r.start >= b:
                    r.start -= d
                    r.end -= d
        self.master_env = env_cut(self.master_env, a, b)
        for k in FX_ATTRS:
            setattr(self, k, env_cut(getattr(self, k), a, b, 0.0, 1.0))

    def insert_time(self, at: float, beats: float):
        """Vloží prázdný čas (pauza před drop)."""
        self.master_env = env_insert(self.master_env, at, beats)
        for k in FX_ATTRS:
            setattr(self, k, env_insert(getattr(self, k), at, beats, 0.0, 1.0))
        for l in self.lanes:
            l.env = env_insert(l.env, at, beats)
            for k in FX_ATTRS:
                setattr(l, k, env_insert(getattr(l, k), at, beats, 0.0, 1.0))
            new = []
            for r in l.regions:
                if r.start >= at:
                    r.start += beats
                    r.end += beats
                    new.append(r)
                elif r.end > at:
                    new.append(Region(r.start, at, r.gain, r.phase, fade_in=r.fade_in))
                    new.append(Region(at + beats, r.end + beats, r.gain, r.origin - at, fade_out=r.fade_out))
                else:
                    new.append(r)
            l.regions = new

    @staticmethod
    def split(lane: Lane, r: Region, at: float) -> Region | None:
        if not (r.start < at < r.end):
            return None
        right = Region(at, r.end, r.gain, r.origin - at, fade_out=r.fade_out)
        r.end = at
        r.fade_out = 0.0
        lane.regions.insert(lane.regions.index(r) + 1, right)
        return right

    def credits_sounds(self) -> list[Sound]:
        seen, out = set(), []
        for l in self.lanes:
            if l.regions and l.key not in seen:
                seen.add(l.key)
                out.append(l.sound())
        return out


# =================================================================== záznam

class Recorder:
    """Sleduje, které zvuky hrají, a skládá z toho Arrangement. Volá ho audio vlákno."""

    def __init__(self, engine):
        self.start_beat = engine.frame / engine.fpb
        self.lanes: dict[str, Lane] = {}
        self.order: list[str] = []
        self.open: dict[str, Region] = {}

    def update(self, e, f: int):
        fpb = e.fpb
        beat = f / fpb - self.start_beat
        audible = {}
        for d in e._decks_t:
            if not d.running_at(f):
                continue
            any_solo = any(g.solo for g in d._groups_t)
            for t in d._live:
                if t.state is None or not t.enabled:
                    continue
                g = e.track_gain(t, d, any_solo)
                if g > 0:
                    audible[f"{d.id}:{t.key}"] = (d, t, g)
        for lid in list(self.open):
            if lid not in audible:
                self.open.pop(lid).end = beat
        for lid, (d, t, g) in audible.items():
            r = self.open.get(lid)
            lane = self.lanes.get(lid)
            if r is not None:
                # změna hlasitosti během hraní -> nový úsek (zachová se jízda faderem)
                if abs(r.gain - g) > 0.03 and beat - r.start >= 0.25:
                    r.end = beat
                    nr = Region(beat, beat, g, r.origin - beat)
                    lane.regions.append(nr)
                    self.open[lid] = nr
                continue
            if lane is None:
                s = t.sound
                lane = Lane(id=lid, key=t.key, title=s.title, ns=s.namespace, file=s.file, deck=d.name,
                            username=s.username, license=s.license, url=s.url,
                            length=t.length, fit=t.fit, pan=t.pan)
                self.lanes[lid] = lane
                self.order.append(lid)
            origin = d.start / fpb - self.start_beat
            nr = Region(beat, beat, g, origin - beat)
            lane.regions.append(nr)
            self.open[lid] = nr

    def finish(self, e) -> Arrangement:
        beat = e.frame / e.fpb - self.start_beat
        for r in self.open.values():
            r.end = beat
        self.open.clear()
        lanes = []
        for lid in self.order:
            l = self.lanes[lid]
            l.regions = [r for r in l.regions if r.end - r.start > 1e-3]
            if l.regions:
                lanes.append(l)
        return Arrangement(bpm=e.bpm, lanes=lanes)


# =================================================================== render

@dataclass
class CompiledLane:
    lane_id: str
    ns: str
    state: tuple
    gains: np.ndarray
    regions: list[tuple]   # start, end, origin, gain, fade_in, fade_out (vzorky)
    env: tuple | None = None   # křivka hlasitosti stopy (env_frames)
    fx_pts: tuple = ([], [])   # křivky úprav stopy v dobách (tame_env, gate_env)
    fx: tuple | None = None    # vypočtená křivka zisku z úprav (vzorky, zisk)


@dataclass
class Compiled:
    sr: int
    bpm: float
    lanes: list[CompiledLane]
    end: int
    master_env: tuple | None = None
    fx_pts: tuple = ([], [])   # křivky úprav celého mixu v dobách
    fx: tuple | None = None    # vypočtená křivka zisku celého mixu


def compile_arr(arr: Arrangement, buffers: dict, sr: int = config.SAMPLE_RATE, loader=None) -> Compiled:
    """Převede úseky v dobách na vzorky a připraví smyčky. loader(lane) -> buffer pro nenačtené zvuky."""
    fpb = sr * 60.0 / arr.bpm
    any_solo = any(l.solo for l in arr.lanes)
    out = []
    for lane in arr.lanes:
        if lane.mute or (any_solo and not lane.solo) or not lane.regions:
            continue
        buf = buffers.get(lane.key)
        if buf is None and loader is not None:
            buf = loader(lane)
        if buf is None:
            continue
        st = prepare_state(buf, sr, arr.bpm, lane.length, lane.fit, bpm_from_title(lane.title))
        regs = region_frames(lane, fpb)
        out.append(CompiledLane(lane.id, lane.ns, st, pan_gains(lane.volume, lane.pan), regs,
                                env_frames(lane.env, fpb), fx_pts(lane)))
    end = max((rg[1] for cl in out for rg in cl.regions), default=0)
    return Compiled(sr, arr.bpm, out, end, env_frames(arr.master_env, fpb), fx_pts(arr))


def fx_pts(owner) -> tuple:
    return tuple([list(p) for p in getattr(owner, k)] for k in FX_ATTRS)


def region_frames(lane: Lane, fpb: float) -> list[tuple]:
    return [(int(round(r.start * fpb)), int(round(r.end * fpb)), int(round(r.origin * fpb)), r.gain,
             int(r.fade_in * fpb), int(r.fade_out * fpb))
            for r in lane.regions if r.end > r.start]


def render_into(c: Compiled, out: np.ndarray, s: int, lane_ids: set[str] | None = None):
    n = len(out)
    e = s + n
    for cl in c.lanes:
        if lane_ids is not None and cl.lane_id not in lane_ids:
            continue
        for rs, re_, origin, g, fi, fo in cl.regions:
            a, b = max(rs, s), min(re_, e)
            if a >= b:
                continue
            env = np.full((b - a, 1), g, np.float32)
            fi = max(FADE_IN, min(fi, re_ - rs))
            fo = max(FADE_OUT, min(fo, re_ - rs))
            if a < rs + fi:
                m = min(b, rs + fi) - a
                env[:m, 0] *= (np.arange(a, a + m) - rs + 1) / fi
            if b > re_ - fo:
                k0 = max(a, re_ - fo)
                env[k0 - a:, 0] *= (re_ - np.arange(k0, b)) / fo
            for ce in (cl.env, c.master_env, cl.fx, c.fx):
                if ce is not None:
                    env[:, 0] *= np.interp(np.arange(a, b, dtype=np.float64), ce[0], ce[1]).astype(np.float32)
            pos = np.arange(a - origin, b - origin, dtype=np.int64)
            mix_loop(out[a - s:b - s], cl.state, pos, env * cl.gains)


def render_to_file(c: Compiled, path: Path, master: float = 0.8, limiter: bool = True,
                   lane_ids: set[str] | None = None, tail_beats: float = 1.0, chunk: int = 65536):
    path.parent.mkdir(parents=True, exist_ok=True)
    total = c.end + int(tail_beats * c.sr * 60 / c.bpm)
    with sf.SoundFile(str(path), "w", c.sr, 2, subtype="PCM_24") as f:
        for s in range(0, total, chunk):
            out = np.zeros((min(chunk, total - s), 2), np.float32)
            render_into(c, out, s, lane_ids)
            out *= master
            if limiter:
                soft_clip(out)
            np.clip(out, -1, 1, out=out)
            f.write(out)


def render_peaks(c: Compiled, bucket: int, master: float = 0.8, chunk: int = 1 << 16) -> np.ndarray:
    """Vlna celého mixu – maximum v každém úseku `bucket` vzorků."""
    total = c.end
    nb = max(1, -(-total // bucket))
    peaks = np.zeros(nb, np.float32)
    chunk = max(bucket, (chunk // bucket) * bucket)
    for s in range(0, total, chunk):
        out = np.zeros((min(chunk, total - s), 2), np.float32)
        render_into(c, out, s)
        out *= master
        soft_clip(out)
        mono = np.abs(out).max(axis=1)
        k = -(-len(mono) // bucket)
        mono = np.pad(mono, (0, k * bucket - len(mono)))
        peaks[s // bucket:s // bucket + k] = mono.reshape(k, bucket).max(axis=1)
    return np.sqrt(np.clip(peaks, 0, 1))


def _amount(pts: list, n: int, fpb: float):
    """Síla úpravy pro každý blok (None = nikde nic)."""
    if not pts or max(v for _, v in pts) <= 0.005:
        return None
    beats = (np.arange(n, dtype=np.float64) + 0.5) * FX_BLOCK / fpb
    return np.clip(np.interp(beats, [q[0] for q in pts], [q[1] for q in pts]), 0.0, 1.0)


def fx_curve(levels: np.ndarray, pts: tuple, sr: int, fpb: float):
    """Křivka zisku z úrovní bloků (FX_BLOCK) a křivek úprav. None = nic se nemění."""
    n = len(levels)
    tame, gate = (_amount(q, n, fpb) for q in pts)
    if n == 0 or (tame is None and gate is None):
        return None
    # běžná hlasitost = medián špiček po dobách (ticho se nepočítá)
    k = max(1, int(round(fpb / FX_BLOCK)))
    m = -(-n // k)
    beats = np.pad(levels, (0, m * k - n)).reshape(m, k).max(axis=1)
    active = beats[beats > max(1e-4, beats.max() * 0.05)]
    if not active.size:
        return None
    typ_db = 20 * np.log10(float(np.median(active)))
    # předstih: blok se upraví už podle špičky v sousedním bloku
    pad = np.pad(levels, 1)
    env = np.maximum(np.maximum(pad[:-2], pad[1:-1]), pad[2:])
    lvl_db = 20 * np.log10(np.maximum(env, 1e-9))
    g_db = np.zeros(n)
    if tame is not None:
        # síla 1/3 ≈ jemně (práh +4 dB, 2:1), 2/3 ≈ středně (+2 dB, 4:1), 1 = silně (0 dB, 20:1)
        thr = typ_db + 6.0 * (1 - tame)
        target = -np.maximum(lvl_db - thr, 0.0) * 0.95 * tame ** 0.6
        rel = TAME_RELEASE_DB_S * FX_BLOCK / sr
        out, prev = np.empty(n), 0.0
        for i, v in enumerate(target.tolist()):     # ztlumení hned, návrat pomalu
            prev = v if v < prev + rel else prev + rel
            out[i] = prev
        g_db += out
    if gate is not None:
        # práh pod běžnou hlasitostí: síla 0 -> -40 dB, 1 -> -12 dB; hloubka až -60 dB
        thr = typ_db - 40.0 + 28.0 * gate
        target = -np.minimum((thr - lvl_db).clip(0.0) * 3.0, 60.0 * gate)
        fall = GATE_FALL_DB_S * FX_BLOCK / sr
        out, prev = np.empty(n), 0.0
        for i, v in enumerate(target.tolist()):     # otevření hned, zavření plynule
            prev = v if v > prev - fall else prev - fall
            out[i] = prev
        g_db += out
    if g_db.min() > -0.05:
        return None
    frames = (np.arange(n, dtype=np.float64) + 0.5) * FX_BLOCK
    return frames, 10 ** (g_db / 20)


def _has_fx(pts: tuple) -> bool:
    return any(q and max(v for _, v in q) > 0.005 for q in pts)


def _scan(c: Compiled, bucket: int, master: float, lane_ids=None, chunk: int = 1 << 16):
    """Projde mix: špičky po sloupcích vlny (bucket) a po blocích FX_BLOCK (před limiterem)."""
    total = c.end
    bpk = np.zeros(max(1, -(-total // bucket)), np.float32)
    levels = np.zeros(max(1, -(-total // FX_BLOCK)), np.float32)
    for s in range(0, total, chunk):
        n = min(chunk, total - s)
        out = np.zeros((n, 2), np.float32)
        render_into(c, out, s, lane_ids)
        mono = np.abs(out).max(axis=1) * master
        for size, arr in ((bucket, bpk), (FX_BLOCK, levels)):
            first = s // size
            starts = np.unique(np.concatenate([[0], np.arange(-s % size, n, size)]))
            vals = np.maximum.reduceat(mono, starts)
            arr[first:first + len(vals)] = np.maximum(arr[first:first + len(vals)], vals)
    return bpk, levels


def _disp(x: np.ndarray) -> np.ndarray:
    return np.sqrt(np.clip(soft_clip(x.astype(np.float32)), 0, 1))


def analyze_mix(c: Compiled, bucket: int, master: float = 0.8):
    """Spočítá úpravy (špičky, šum) stop i mixu a vlnu mixu před a po nich.
    Vrací (vlna před, vlna po, {lane_id: křivka zisku}, křivka zisku mixu)."""
    fpb = c.sr * 60.0 / c.bpm
    base = replace(c, fx=None, lanes=[replace(cl, fx=None) for cl in c.lanes])
    raw, raw_lv = _scan(base, bucket, master)
    lane_fx = {}
    solo = replace(base, master_env=None)
    for cl in base.lanes:
        if _has_fx(cl.fx_pts):
            _, lv = _scan(solo, bucket, 1.0, {cl.lane_id})
            curve = fx_curve(lv, cl.fx_pts, c.sr, fpb)
            if curve is not None:
                lane_fx[cl.lane_id] = curve
    mid = replace(base, lanes=[replace(cl, fx=lane_fx.get(cl.lane_id)) for cl in base.lanes])
    master_fx = None
    if _has_fx(c.fx_pts):
        lv = _scan(mid, bucket, master)[1] if lane_fx else raw_lv
        master_fx = fx_curve(lv, c.fx_pts, c.sr, fpb)
    if not lane_fx and master_fx is None:
        p = _disp(raw)
        return p, p, lane_fx, None
    pk, _ = _scan(replace(mid, fx=master_fx), bucket, master)
    return _disp(raw), _disp(pk), lane_fx, master_fx


def with_fx(c: Compiled, master: float = 0.8) -> Compiled:
    """Compiled s vypočtenými úpravami (pro export)."""
    bucket = max(1, int(c.sr * 60.0 / c.bpm / 16))
    _, _, lane_fx, master_fx = analyze_mix(c, bucket, master)
    return replace(c, fx=master_fx, lanes=[replace(cl, fx=lane_fx.get(cl.lane_id)) for cl in c.lanes])


class ArrPlayer:
    """Přehrávání postprodukce přes engine (engine.arr_player)."""

    def __init__(self):
        self.compiled: Compiled | None = None
        self.pos = 0
        self.playing = False

    def render(self, out: np.ndarray):
        c = self.compiled
        if c is None:
            self.playing = False
            return
        render_into(c, out, self.pos)
        self.pos += len(out)
        if self.pos >= c.end + c.sr:
            self.playing = False


def bars_beats(beat: float) -> str:
    bar, b = divmod(beat, BEATS_PER_BAR)
    return f"{int(bar) + 1}.{int(b) + 1}"

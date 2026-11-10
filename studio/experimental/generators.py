"""Co vzorce vyrábí – čisté funkce (bez GUI), aby šly zkoušet i testovat samostatně.

  rhythm()          – rytmus ze zvuku: vzorec rozhodne, na kterých krocích zazní (a jak hlasitě)
  synth()           – zvuk přímo ze vzorce (vlna v čase t, nebo bytebeat)
  curve_points()    – body křivky pro postprodukci (hlasitost / špičky / šum)
  autopilot_step()  – které zvuky v mixu mají hrát v příštím úseku
"""
from __future__ import annotations

import numpy as np

from ..library import apply_edge_fades, resample
from .formula import Formula

BYTEBEAT_SR = 8000
CHOKE_FADE = 64

RHYTHM_VARS = {"n", "krok", "takt", "doba", "kroku", "bpm"}
SYNTH_VARS = {"t", "k", "doba", "bpm", "f"}
CURVE_VARS = {"t", "x", "takt", "delka"}
AUTOPILOT_VARS = {"i", "N", "zap", "vlevo", "vpravo", "pocet", "takt"}

RHYTHM_EXAMPLES = [
    ("Každá doba (four on the floor)", "krok % 4 == 0"),
    ("Mezi doby (offbeat)", "krok % 4 == 2"),
    ("Euklidovský 5 z 16", "euklid(5, 16)"),
    ("Euklidovský 3 z 8, posunutý", "euklid(3, 8, 2)"),
    ("Každá třetí šestnáctina", "n % 3 == 0"),
    ("Náhodně 30 %", "nahoda() < 0.3"),
    ("Akcent na první době, jinak tišeji", "where(krok == 0, 1, 0.4) * (krok % 2 == 0)"),
    ("Zahušťování během taktů", "nahoda() < (takt + 1) / 4"),
    ("Sinusová vlna úderů", "sin(n / 3) > 0.4"),
    ("Polyrytmus 3 proti 4", "(n % 3 == 0) or (n % 4 == 0)"),
]
SYNTH_EXAMPLES = [
    ("Kick (klesající tón)", "sin(2*pi*(50*t + 120*(1 - exp(-25*t))/25)) * exp(-6*t)", "vlna"),
    ("Hi-hat (šum, krátký)", "nahoda()*2 - 1 if t < 0.05 else 0", "vlna"),
    ("Basa (pila 55 Hz, pulzuje po dobách)", "saw(55*t) * exp(-4*(t % doba)) * 0.6", "vlna"),
    ("Akord (3 sinusovky)", "(sin(2*pi*220*t) + sin(2*pi*277*t) + sin(2*pi*330*t)) / 3 * 0.6", "vlna"),
    ("Stereo rozladění", "sin(2*pi*(110 + k*1.5)*t) * 0.6", "vlna"),
    ("FM zvon", "sin(2*pi*440*t + 3*sin(2*pi*660*t)*exp(-3*t)) * exp(-2*t)", "vlna"),
    ("Bytebeat klasik", "t*(t >> 5 | t >> 8)", "bytebeat"),
    ("Bytebeat 2", "t*((t >> 12 | t >> 8) & 63 & t >> 4)", "bytebeat"),
    ("Bytebeat 3", "(t*5 & t >> 7) | (t*3 & t >> 10)", "bytebeat"),
]
CURVE_EXAMPLES = [
    ("Vlnění po 2 taktech", "0.5 + 0.5*sin(2*pi*t/8)"),
    ("Pomalý nájezd přes celý výběr", "x"),
    ("Vyjetí (fade out)", "1 - x"),
    ("Exponenciální nájezd", "x**3"),
    ("Pumpování po dobách (sidechain)", "0.3 + 0.7*min(1, (t % 1) * 3)"),
    ("Schody po taktech", "floor(takt) / floor(delka / 4)"),
    ("Brány: každý druhý takt", "floor(takt) % 2"),
]
AUTOPILOT_EXAMPLES = [
    ("Buněčný automat – pravidlo 30 (chaos)", "pravidlo(30)"),
    ("Buněčný automat – pravidlo 90 (fraktál)", "pravidlo(90)"),
    ("Buněčný automat – pravidlo 110", "pravidlo(110)"),
    ("Náhodně, asi třetina zvuků", "nahoda() < 0.33"),
    ("Náhodná změna jen u pár zvuků", "where(nahoda() < 0.15, 1 - zap, zap)"),
    ("Vlna putující přes zvuky", "sin(takt/4 + i) > 0.3"),
    ("Postupně přibývá, pak ubývá", "i < N * (0.5 + 0.5*sin(takt/8))"),
]


def euclid_hits(n: np.ndarray, k, m, r=0) -> np.ndarray:
    """Euklidovský rytmus: k úderů rovnoměrně rozložených v m krocích, posun r."""
    m = np.maximum(1, np.asarray(m, np.int64))
    pos = (np.asarray(n, np.int64) + np.asarray(r, np.int64)) % m
    return ((pos * np.asarray(k, np.int64)) % m) < np.asarray(k, np.int64)


def _random(seed: int, shape):
    rng = np.random.default_rng(seed)
    return lambda: rng.random(shape)


# ------------------------------------------------------------------ rytmus

def rhythm_pattern(text: str, bars: int, steps: int, bpm: float, seed: int = 0) -> np.ndarray:
    """Hlasitost úderu pro každý krok (0 = nezazní)."""
    f = Formula(text, RHYTHM_VARS, {"euklid", "nahoda"})
    total = bars * steps
    n = np.arange(total, dtype=np.int64)
    ctx = {"n": n, "krok": n % steps, "takt": n // steps, "doba": (n * 4 // steps) % 4,
           "kroku": np.int64(steps), "bpm": float(bpm)}
    funcs = {"euklid": lambda k, m, r=0: euclid_hits(n, k, m, r), "nahoda": _random(seed, (total,))}
    return np.clip(f.eval(ctx, (total,), funcs), 0.0, 2.0)


def rhythm(buf: np.ndarray, pattern: np.ndarray, bars: int, bpm: float, sr: int, choke: bool = False) -> np.ndarray:
    """Vyrenderuje smyčku (bars taktů): na krocích s nenulovou hlasitostí zazní `buf`.
    Konec smyčky přetéká na začátek, takže dozvuk posledního úderu není useknutý."""
    total = int(round(bars * 4 * sr * 60.0 / bpm))
    out = np.zeros((total, 2), np.float32)
    steps = len(pattern)
    hits = [(int(round(i * total / steps)), float(g)) for i, g in enumerate(pattern) if g > 0]
    for j, (start, g) in enumerate(hits):
        length = min(len(buf), total)
        if choke and len(hits) > 1:
            nxt = hits[(j + 1) % len(hits)][0]
            gap = (nxt - start) % total or total
            length = min(length, gap)
        seg = buf[:length] * g
        if choke and length < len(buf):
            k = min(CHOKE_FADE, length)
            seg[length - k:] *= np.linspace(1, 0, k, dtype=np.float32)[:, None]
        a = min(length, total - start)
        out[start:start + a] += seg[:a]
        if a < length:                       # přetečení na začátek smyčky
            out[:length - a] += seg[a:]
    return _normalize(out)


# ------------------------------------------------------------------ zvuk ze vzorce

def synth(text: str, mode: str, seconds: float, sr: int, bpm: float, seed: int = 0) -> np.ndarray:
    """mode 'vlna': t v sekundách, výsledek -1..1. mode 'bytebeat': t = celé číslo (8000/s),
    výsledek & 255 (klasický bytebeat). k = kanál (0 levý, 1 pravý)."""
    f = Formula(text, SYNTH_VARS, {"nahoda"})
    rate = BYTEBEAT_SR if mode == "bytebeat" else sr
    n = max(1, int(round(seconds * rate)))
    shape = (n, 2)
    k = np.array([[0, 1]], np.int64)
    if mode == "bytebeat":
        t = np.arange(n, dtype=np.int64)[:, None]
    else:
        t = (np.arange(n, dtype=np.float64) / sr)[:, None]
    ctx = {"t": t, "k": k, "doba": 60.0 / bpm, "bpm": float(bpm), "f": float(rate)}
    v = f.eval(ctx, shape, {"nahoda": _random(seed, shape)})
    if mode == "bytebeat":
        v = (v.astype(np.int64) & 255).astype(np.float64) / 127.5 - 1.0
        v -= v.mean(axis=0)
        out = resample(v.astype(np.float32), int(round(n * sr / rate)))
    else:
        out = np.clip(v, -1.0, 1.0).astype(np.float32)
    if not np.abs(out).max() > 1e-6:
        raise ValueError("Vzorec dává jen ticho.")
    return apply_edge_fades(_normalize(np.ascontiguousarray(out)), sr, 0.5, 5.0)


def _normalize(out: np.ndarray, peak: float = 0.95) -> np.ndarray:
    m = float(np.abs(out).max()) if len(out) else 0.0
    if m > peak:
        out *= peak / m
    return out


# ------------------------------------------------------------------ křivky

def curve_points(text: str, a: float, b: float, step: float, vmax: float) -> list[list[float]]:
    """Body křivky od doby a do doby b. t = doby od začátku výběru, x = 0..1, takt = t/4."""
    f = Formula(text, CURVE_VARS)
    if b <= a:
        raise ValueError("Konec musí být za začátkem.")
    step = max(1 / 64, step)
    t = np.arange(0.0, b - a + 1e-9, step)
    if t[-1] < b - a - 1e-9:
        t = np.append(t, b - a)
    ctx = {"t": t, "x": t / (b - a), "takt": t / 4, "delka": float(b - a)}
    v = np.clip(f.eval(ctx, t.shape), 0.0, vmax)
    return [[float(a + ti), round(float(vi), 4)] for ti, vi in zip(t, v)]


def merge_curve(old: list, new: list, a: float, b: float) -> list:
    """Nahradí body ve výběru [a, b] novými, ostatní nechá."""
    keep = [list(p) for p in old if p[0] < a - 1e-9 or p[0] > b + 1e-9]
    return sorted(keep + new, key=lambda p: p[0])


# ------------------------------------------------------------------ autopilot

def autopilot_step(text: str, states: np.ndarray, takt: int, seed: int = 0) -> np.ndarray:
    """Nové zapnutí zvuků (bool) podle vzorce. zap/vlevo/vpravo = současný stav (0/1)."""
    f = Formula(text, AUTOPILOT_VARS, {"pravidlo", "nahoda"})
    n = len(states)
    if n == 0:
        return np.zeros(0, bool)
    zap = np.asarray(states, np.int64)
    vl, vp = np.roll(zap, 1), np.roll(zap, -1)
    ctx = {"i": np.arange(n, dtype=np.int64), "N": np.int64(n), "zap": zap, "vlevo": vl, "vpravo": vp,
           "pocet": np.int64(zap.sum()), "takt": np.int64(takt)}

    def pravidlo(code):
        """Elementární buněčný automat (Wolframovo číslo pravidla 0–255)."""
        return (np.asarray(code, np.int64) >> (vl * 4 + zap * 2 + vp)) & 1

    v = f.eval(ctx, (n,), {"pravidlo": pravidlo, "nahoda": _random(seed + takt, (n,))})
    return v > 0

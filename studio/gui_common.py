"""Společné věci pro GUI: styl, pomocné widgety, načítání audia a vln na pozadí."""
from __future__ import annotations

import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QPushButton, QSlider

from . import library
from .engine import Engine
from .library import Sound
from .waveform import PeakCache, compute_peaks

STYLE = """
QPushButton { padding: 4px 9px; }   /* šířka podle textu, ne min. 80 px – okno jde zmenšit */
QPushButton[small="true"] { padding: 2px 0px; }   /* úzká tlačítka M, S, ✕, −, + */
QPushButton#mute:checked { background: #b33; color: white; }
QPushButton#solo:checked { background: #c9a227; color: black; }
QPushButton#rec:checked  { background: #c62828; color: white; font-weight: bold; }
QPushButton#play:checked { background: #1f8f45; color: white; font-weight: bold; }
QPushButton#vol:checked  { background: #c9a227; color: black; font-weight: bold; }
QPushButton#one:checked  { background: #2f7de1; color: white; font-weight: bold; }
QPushButton#brk:checked  { background: #7b3fc4; color: white; font-weight: bold; }
QPushButton[pending="true"] { border: 2px solid #f0c040; }   /* změna čeká na takt */
QPushButton#big { font-size: 14px; font-weight: bold; padding: 6px 14px; }
QFrame#column { background: #1e2023; border: 1px solid #33373c; border-radius: 6px; }
QFrame#card { background: #1e2023; border: 1px solid #33373c; border-radius: 6px; }
QLabel#colname { font-weight: bold; font-size: 14px; }
QLabel#status { border-radius: 4px; padding: 2px 8px; font-weight: bold; }
QToolButton#len { min-width: 38px; }
QToolButton#fit:checked { background: #7b3fc4; color: white; }
"""

STATUS_STYLE = {
    "stopped": ("stojí", "#33373c", "#aab"),
    "waiting": ("čeká na takt", "#8a6a12", "white"),
    "playing": ("hraje", "#1f8f45", "white"),
    "stopping": ("končí", "#a5521b", "white"),
}


def nofocus(w):
    w.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return w


def mkbtn(text, checkable=False, name=None, tip=None, width=None):
    b = nofocus(QPushButton(text))
    if width:
        b.setFixedWidth(width)
        b.setProperty("small", True)
    b.setCheckable(checkable)
    if name:
        b.setObjectName(name)
    if tip:
        b.setToolTip(tip)
    return b


def mkslider(value, maximum=150, width=None, tip=None):
    s = nofocus(QSlider(Qt.Orientation.Horizontal))
    s.setRange(0, maximum)
    s.setValue(value)
    if width:
        s.setFixedWidth(width)
    if tip:
        s.setToolTip(tip)
    return s


def set_checked(w, value: bool):
    w.blockSignals(True)
    w.setChecked(value)
    w.blockSignals(False)


def set_value(w, value):
    w.blockSignals(True)
    w.setValue(value)
    w.blockSignals(False)


def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)


def lane_color(name: str) -> QColor:
    h = (zlib.crc32(name.encode()) * 47) % 360
    return QColor.fromHsv(h, 150, 205)


def write_credits(path: Path, sounds: list[Sound]):
    lines = ["Použité zvuky (freesound.org). Licence CC-BY vyžadují uvedení autora.", ""]
    for s in sorted(sounds, key=lambda s: (s.namespace, s.title.lower())):
        lines.append(f"- \"{s.title}\" by {s.username or '?'} [{s.license or '?'}] {s.url}  ({s.namespace})")
    path.write_text("\n".join(lines) + "\n", "utf-8")


def dark_palette(app: QApplication):
    app.setStyle("Fusion")
    p = QPalette()
    c = {
        QPalette.ColorRole.Window: "#16181b", QPalette.ColorRole.WindowText: "#d8dce0",
        QPalette.ColorRole.Base: "#111315", QPalette.ColorRole.AlternateBase: "#1b1e21",
        QPalette.ColorRole.Text: "#d8dce0", QPalette.ColorRole.Button: "#25282c",
        QPalette.ColorRole.ButtonText: "#d8dce0", QPalette.ColorRole.Highlight: "#2f7de1",
        QPalette.ColorRole.HighlightedText: "#ffffff", QPalette.ColorRole.ToolTipBase: "#25282c",
        QPalette.ColorRole.ToolTipText: "#e8e8e8", QPalette.ColorRole.PlaceholderText: "#6b7178",
    }
    for role, col in c.items():
        p.setColor(role, QColor(col))
    app.setPalette(p)
    app.setStyleSheet(STYLE)


class AudioService(QObject):
    """Načítání zvuků (pro přehrávání) a výpočet vln na pozadí."""
    loaded = Signal(str)          # audio je v enginu
    failed = Signal(str, str)
    peaks_ready = Signal(str)
    _buf = Signal(str, object, object)
    _pk = Signal(str, object, object)

    def __init__(self, engine: Engine):
        super().__init__()
        self.engine = engine
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.peak_pool = ThreadPoolExecutor(max_workers=2)
        self.loading: set[str] = set()
        self.peak_pending: set[str] = set()
        self.peak_failed: set[str] = set()
        self.peaks = PeakCache()
        self._buf.connect(self._on_buf)
        self._pk.connect(self._on_pk)

    def ensure_loaded(self, sound: Sound):
        key = sound.key
        if key in self.engine.buffers or key in self.loading:
            return
        self.loading.add(key)

        def job():
            try:
                buf = library.load_audio(sound.path)
            except Exception as e:
                buf = e
            self._buf.emit(key, buf, sound)

        self.pool.submit(job)

    def load_sync(self, sound: Sound):
        buf = self.engine.buffers.get(sound.key)
        if buf is None:
            try:
                buf = library.load_audio(sound.path)
            except Exception:
                return None
            self.engine.set_buffer(sound.key, buf)
            if self.peaks.get(sound) is None:
                self.peaks.put(sound, compute_peaks(buf))
        return buf

    def _on_buf(self, key, buf, sound):
        self.loading.discard(key)
        if isinstance(buf, Exception):
            for d in self.engine._decks_t:
                t = d.tracks.get(key)
                if t:
                    t.want = False
            self.failed.emit(key, str(buf))
            return
        if self.peaks.get(sound) is None:
            self.peaks.put(sound, compute_peaks(buf))
            self.peaks_ready.emit(key)
        self.engine.set_buffer(key, buf)
        for d in self.engine._decks_t:
            t = d.tracks.get(key)
            if t is not None and t.want:
                t.want = False
                self.engine.request(t, True)
        self.loaded.emit(key)

    def request_peaks(self, sound: Sound):
        key = sound.key
        if (key in self.peak_pending or key in self.loading or key in self.peak_failed
                or self.peaks.get(sound) is not None):
            return
        buf = self.engine.buffers.get(key)
        if buf is not None:
            self.peaks.put(sound, compute_peaks(buf))
            self.peaks_ready.emit(key)
            return
        self.peak_pending.add(key)

        def job():
            try:
                pk = compute_peaks(library.load_audio(sound.path))
            except Exception:
                pk = None
            self._pk.emit(key, pk, sound)

        self.peak_pool.submit(job)

    def _on_pk(self, key, pk, sound):
        self.peak_pending.discard(key)
        if pk is None:
            self.peak_failed.add(key)
        else:
            self.peaks.put(sound, pk)
            self.peaks_ready.emit(key)

    def shutdown(self):
        self.peaks.flush()
        self.pool.shutdown(wait=False, cancel_futures=True)
        self.peak_pool.shutdown(wait=False, cancel_futures=True)

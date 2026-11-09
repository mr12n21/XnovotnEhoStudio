"""GUI – PySide6."""
from __future__ import annotations

import json
import random
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QKeySequence, QPalette, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QFrame, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider,
    QSpinBox, QSplitter, QToolButton, QVBoxLayout, QWidget,
)

from . import config, library, scraper
from .audio_out import create_output
from .engine import BEATS_PER_BAR, LENGTH_OPTIONS, Engine, Track, length_label
from .library import Sound

QUANTIZE = [("off", "Okamžitě"), ("beat", "Na dobu"), ("bar", "Na takt")]
N_SCENES = 8

STYLE = """
QPushButton#track { text-align: left; padding: 3px 6px; background: #2a2d31;
    border: 1px solid #3a3f45; border-radius: 3px; color: #cfd3d8; }
QPushButton#track:hover { border-color: #6a7480; }
QPushButton#track[st="on"]   { background: #1f8f45; color: white; border-color: #33c06a; }
QPushButton#track[st="pend"] { background: #8a6a12; color: white; }
QPushButton#track[st="off-pend"] { background: #5a3a12; color: white; }
QPushButton#track[st="load"] { background: #1d4f8c; color: white; }
QPushButton#mute:checked { background: #b33; color: white; }
QPushButton#solo:checked { background: #c9a227; color: black; }
QPushButton#rec:checked  { background: #c62828; color: white; font-weight: bold; }
QPushButton#play:checked { background: #1f8f45; color: white; font-weight: bold; }
QPushButton#scene { min-width: 34px; }
QPushButton#scene[filled="1"] { border: 1px solid #4fa3ff; color: #9fd0ff; }
QFrame#column { background: #1e2023; border: 1px solid #33373c; border-radius: 6px; }
QLabel#colname { font-weight: bold; font-size: 14px; }
QToolButton#len { min-width: 38px; }
QToolButton#fit:checked { background: #7b3fc4; color: white; }
"""


def nofocus(w):
    w.setFocusPolicy(Qt.FocusPolicy.NoFocus)
    return w


def mkbtn(text, checkable=False, name=None, tip=None):
    b = nofocus(QPushButton(text))
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


def repolish(w):
    w.style().unpolish(w)
    w.style().polish(w)


def write_credits(path: Path, sounds: list[Sound]):
    lines = ["Použité zvuky (freesound.org). Licence CC-BY vyžadují uvedení autora.", ""]
    for s in sorted(sounds, key=lambda s: (s.namespace, s.title.lower())):
        lines.append(f"- \"{s.title}\" by {s.username or '?'} [{s.license or '?'}] {s.url}  ({s.namespace})")
    path.write_text("\n".join(lines) + "\n", "utf-8")


# ======================================================================= vlákna

class Bridge(QObject):
    loaded = Signal(str, object)


class DownloadThread(QThread):
    log = Signal(str)
    progress = Signal(int, int)
    ns_done = Signal(str)

    def __init__(self, jobs: list[tuple[str, str]], pages: int, max_dur: float):
        super().__init__()
        self.jobs, self.pages, self.max_dur = jobs, pages, max_dur
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        for url, ns in self.jobs:
            if self._stop:
                break
            self.log.emit(f"=== {ns}  ←  {url}")
            try:
                scraper.download(url, ns, self.pages, self.max_dur, log=self.log.emit,
                                 progress=self.progress.emit, should_stop=lambda: self._stop)
            except Exception as e:
                self.log.emit(f"CHYBA: {e}")
            self.ns_done.emit(ns)


# ======================================================================= mixer widgety

class TrackRow(QWidget):
    def __init__(self, main: "MainWindow", t: Track):
        super().__init__()
        self.main, self.t = main, t
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)

        self.btn = mkbtn("", True, "track")
        self.btn.setMinimumWidth(120)
        self.btn.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.btn.clicked.connect(lambda on: main.toggle(self.t, on))
        self.btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.btn.customContextMenuRequested.connect(self._menu)

        self.len = nofocus(QToolButton())
        self.len.setObjectName("len")
        self.len.setToolTip("Délka smyčky v dobách (auto = zaokrouhlit nahoru, free = přirozená délka)")
        self.len.clicked.connect(self._len_menu)

        self.fit = nofocus(QToolButton())
        self.fit.setObjectName("fit")
        self.fit.setText("⇔")
        self.fit.setCheckable(True)
        self.fit.setToolTip("Napasovat zvuk přesně do tempa (přetáhne ho – mění výšku tónu)")
        self.fit.toggled.connect(self._set_fit)

        self.vol = mkslider(int(t.volume * 100), width=64, tip="Hlasitost zvuku")
        self.vol.valueChanged.connect(lambda v: setattr(self.t, "volume", v / 100))

        lay.addWidget(self.btn, 1)
        lay.addWidget(self.len)
        lay.addWidget(self.fit)
        lay.addWidget(self.vol)
        self.sync_controls()

    def sync_controls(self):
        t = self.t
        self.vol.blockSignals(True)
        self.vol.setValue(int(t.volume * 100))
        self.vol.blockSignals(False)
        self.fit.blockSignals(True)
        self.fit.setChecked(t.fit)
        self.fit.blockSignals(False)
        self.refresh()

    def refresh(self):
        t = self.t
        if t.loading:
            st = "load"
        elif t.pending is not None:
            st = "pend" if t.pending[0] else "off-pend"
        elif t.enabled:
            st = "on"
        else:
            st = ""
        self.btn.blockSignals(True)
        self.btn.setChecked(t.on)
        self.btn.blockSignals(False)
        if self.btn.property("st") != st:
            self.btn.setProperty("st", st)
            repolish(self.btn)
        fm = self.btn.fontMetrics()
        title = t.sound.title
        elided = fm.elidedText(title, Qt.TextElideMode.ElideRight, max(60, self.btn.width() - 14))
        self.btn.setText(elided.replace("&", "&&"))
        beats = self.main.engine.loop_beats(t)
        if t.length == "auto" and beats is not None:
            self.len.setText(f"a{length_label(beats)}")
        else:
            self.len.setText(length_label(t.length))
        s = t.sound
        info = [f"<b>{title}</b>", f"autor: {s.username or '?'}", f"licence: {s.license or '?'}"]
        if t.buffer is not None:
            info.append(f"délka: {len(t.buffer) / self.main.engine.sr:.2f} s")
        elif s.duration:
            info.append(f"délka: {s.duration:.2f} s")
        if beats is not None:
            info.append(f"smyčka: {length_label(beats)} dob")
        if t.src_bpm:
            info.append(f"tempo v názvu: {t.src_bpm:g} BPM")
        info.append("<i>pravé tlačítko = další volby</i>")
        self.btn.setToolTip("<br>".join(info))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self.refresh()

    def _len_menu(self):
        m = QMenu(self)
        for opt in LENGTH_OPTIONS:
            a = m.addAction(length_label(opt) + ("" if opt in ("auto", "free") else " dob"))
            a.setCheckable(True)
            a.setChecked(opt == self.t.length)
            a.triggered.connect(lambda _=False, o=opt: self._set_len(o))
        m.exec(self.len.mapToGlobal(self.len.rect().bottomLeft()))

    def _set_len(self, opt):
        self.t.length = opt
        self.main.engine.prepare(self.t)
        self.refresh()

    def _set_fit(self, on):
        self.t.fit = on
        self.main.engine.prepare(self.t)
        self.refresh()

    def _menu(self, pos):
        t, s = self.t, self.t.sound
        m = QMenu(self)
        if s.url:
            m.addAction("Otevřít na freesound.org", lambda: QDesktopServices.openUrl(QUrl(s.url)))
        pan = m.addMenu("Panorama")
        for label, v in [("Vlevo", -0.7), ("Střed", 0.0), ("Vpravo", 0.7)]:
            a = pan.addAction(label, lambda v=v: setattr(t, "pan", v))
            a.setCheckable(True)
            a.setChecked(abs(t.pan - v) < 0.01)
        cp = m.addMenu("Kopírovat do kategorie")
        for ns in self.main.lib:
            if ns != s.namespace:
                cp.addAction(ns, lambda ns=ns: self.main.copy_to(s, ns))
        cp.addSeparator()
        cp.addAction("Nová kategorie…", lambda: self.main.copy_to(s, None))
        m.addSeparator()
        m.addAction("Smazat z knihovny", lambda: self.main.delete_sound(t))
        m.exec(self.btn.mapToGlobal(pos))


class GroupColumn(QFrame):
    def __init__(self, main: "MainWindow", ns: str):
        super().__init__()
        self.main, self.ns = main, ns
        self.g = main.engine.group(ns)
        self.rows: dict[str, TrackRow] = {}
        self.setObjectName("column")
        self.setFixedWidth(340)
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)

        top = QHBoxLayout()
        self.name = QLabel(ns)
        self.name.setObjectName("colname")
        self.count = QLabel()
        close = mkbtn("✕", tip="Zavřít sloupec (zvuky zůstanou v knihovně)")
        close.setFixedWidth(26)
        close.clicked.connect(lambda: main.set_column_open(ns, False))
        top.addWidget(self.name, 1)
        top.addWidget(self.count)
        top.addWidget(close)
        v.addLayout(top)

        ctl = QHBoxLayout()
        self.mute = mkbtn("M", True, "mute", "Ztlumit kategorii")
        self.solo = mkbtn("S", True, "solo", "Sólo kategorie")
        for b in (self.mute, self.solo):
            b.setFixedWidth(30)
        self.mute.toggled.connect(lambda on: setattr(self.g, "mute", on))
        self.solo.toggled.connect(lambda on: setattr(self.g, "solo", on))
        rnd = mkbtn("Náh.", tip="Náhodně zapnout 3 zvuky z kategorie (ostatní vypnout)")
        rnd.setFixedWidth(42)
        rnd.clicked.connect(self.randomize)
        off = mkbtn("Vyp", tip="Vypnout všechny zvuky v kategorii")
        off.setFixedWidth(40)
        off.clicked.connect(lambda: [main.toggle(r.t, False) for r in self.rows.values() if r.t.on])
        self.vol = mkslider(int(self.g.volume * 100), tip="Hlasitost kategorie")
        self.vol.valueChanged.connect(lambda val: setattr(self.g, "volume", val / 100))
        for w in (self.mute, self.solo, rnd, off):
            ctl.addWidget(w)
        ctl.addWidget(self.vol, 1)
        v.addLayout(ctl)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filtr…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self.apply_filter)
        v.addWidget(self.filter)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.inner = QWidget()
        self.list = QVBoxLayout(self.inner)
        self.list.setContentsMargins(0, 0, 4, 0)
        self.list.setSpacing(2)
        self.list.addStretch(1)
        self.scroll.setWidget(self.inner)
        v.addWidget(self.scroll, 1)
        self.sync_controls()
        self.set_sounds(main.lib.get(ns, []))

    def sync_controls(self):
        for w, val in ((self.mute, self.g.mute), (self.solo, self.g.solo)):
            w.blockSignals(True)
            w.setChecked(val)
            w.blockSignals(False)
        self.vol.blockSignals(True)
        self.vol.setValue(int(self.g.volume * 100))
        self.vol.blockSignals(False)

    def set_sounds(self, sounds: list[Sound]):
        keys = [s.key for s in sounds]
        for k in list(self.rows):
            if k not in keys:
                self.rows.pop(k).deleteLater()
        for i, s in enumerate(sounds):
            row = self.rows.get(s.key)
            if row is None:
                row = TrackRow(self.main, self.main.engine.tracks[s.key])
                self.rows[s.key] = row
            self.list.insertWidget(i, row)
        self.apply_filter(self.filter.text())
        self.update_count()

    def apply_filter(self, text):
        words = text.lower().split()
        for r in self.rows.values():
            hay = f"{r.t.sound.title} {r.t.sound.username}".lower()
            r.setVisible(all(w in hay for w in words))

    def update_count(self):
        on = sum(1 for r in self.rows.values() if r.t.on)
        self.count.setText(f"{on}/{len(self.rows)}")

    def randomize(self):
        visible = [r.t for r in self.rows.values() if r.isVisible()]
        pick = set(random.sample(visible, min(3, len(visible))))
        for r in self.rows.values():
            want = r.t in pick
            if r.t.on != want:
                self.main.toggle(r.t, want)


class ExportDialog(QDialog):
    def __init__(self, parent, n_tracks: int, bpm: float):
        super().__init__(parent)
        self.setWindowTitle("Export")
        f = QFormLayout(self)
        f.addRow(QLabel(f"Aktivních zvuků: <b>{n_tracks}</b> · tempo {bpm:g} BPM"))
        self.bars = QSpinBox()
        self.bars.setRange(1, 1024)
        self.bars.setValue(32)
        self.bars.setSuffix(" taktů")
        f.addRow("Délka:", self.bars)
        self.mix = QCheckBox("Mix (stereo WAV 24 bit)")
        self.mix.setChecked(True)
        self.stems = QCheckBox("Stopy po kategoriích (stems)")
        self.stems.setChecked(True)
        self.loops = QCheckBox("Jednotlivé smyčky (1 opakování, pro DAW)")
        for w in (self.mix, self.stems, self.loops):
            f.addRow(w)
        self.folder = QLineEdit(str(config.EXPORT_DIR / datetime.now().strftime("mix_%Y%m%d_%H%M%S")))
        f.addRow("Složka:", self.folder)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)


# ======================================================================= hlavní okno

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Novotného Sound Studio")
        self.resize(1500, 900)
        self.engine = Engine()
        self.output = create_output(self.engine)
        self.bridge = Bridge()
        self.bridge.loaded.connect(self._on_loaded)
        self.pool = ThreadPoolExecutor(max_workers=4)
        self.lib: dict[str, list[Sound]] = {}
        self.columns: dict[str, GroupColumn] = {}
        self.scenes: dict[str, list[str]] = {}
        self.dl: DownloadThread | None = None
        self.watch: set[str] = set()
        self.rec_started = 0.0

        self._build_ui()
        self._build_menu()
        self.rescan()
        if not self.load_session(config.SESSION_FILE):
            for ns in list(self.lib)[:6]:
                self.set_column_open(ns, True)
        self.output.start()
        self.statusBar().showMessage(f"Zvukový výstup: {self.output.name}  ·  knihovna: {config.LIBRARY_DIR}")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(40)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 4)

        # --- transport
        tr = QHBoxLayout()
        self.play_btn = mkbtn("▶  Play", True, "play", "Play / Stop (mezerník)")
        self.play_btn.setMinimumWidth(100)
        self.play_btn.toggled.connect(self.set_playing)
        self.bpm = nofocus(QDoubleSpinBox())
        self.bpm.setRange(40, 250)
        self.bpm.setDecimals(1)
        self.bpm.setValue(self.engine.bpm)
        self.bpm.setSuffix(" BPM")
        self.bpm.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.bpm.valueChanged.connect(self._bpm_changed)
        self.quant = nofocus(QComboBox())
        for k, label in QUANTIZE:
            self.quant.addItem(label, k)
        self.quant.setCurrentIndex(1)
        self.quant.setToolTip("Kdy se zapnutí/vypnutí zvuku projeví")
        self.quant.currentIndexChanged.connect(lambda i: setattr(self.engine, "quantize", self.quant.itemData(i)))
        self.pos = QLabel()
        self.pos.setMinimumWidth(150)
        self.pos.setStyleSheet("font-family: monospace; font-size: 15px;")
        self.master = mkslider(int(self.engine.master * 100), tip="Master hlasitost")
        self.master.setFixedWidth(160)
        self.master.valueChanged.connect(lambda v: setattr(self.engine, "master", v / 100))
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.setFixedWidth(140)
        self.meter.setFixedHeight(12)
        self.rec_btn = mkbtn("● REC", True, "rec", "Nahrávat výstup do WAV (živé hraní)")
        self.rec_btn.toggled.connect(self.set_recording)
        exp = mkbtn("Export…", tip="Vyrenderovat aktuální stav do WAV (mix / stopy / smyčky)")
        exp.clicked.connect(self.export)
        panic = mkbtn("Vše vypnout", tip="Vypnout všechny zvuky (Esc)")
        panic.clicked.connect(self.all_off)
        for w in (self.play_btn, self.bpm, self.quant, self.pos):
            tr.addWidget(w)
        tr.addSpacing(12)
        tr.addWidget(QLabel("Master"))
        tr.addWidget(self.master)
        tr.addWidget(self.meter)
        tr.addStretch(1)
        for w in (panic, self.rec_btn, exp):
            tr.addWidget(w)
        root.addLayout(tr)

        # --- scény
        sc = QHBoxLayout()
        sc.addWidget(QLabel("Scény:"))
        self.scene_btns = []
        for i in range(1, N_SCENES + 1):
            b = mkbtn(str(i), name="scene",
                      tip=f"Scéna {i}: klik = vyvolat · Ctrl+klik nebo pravé tlačítko = uložit aktuální stav")
            b.clicked.connect(lambda _=False, i=i: self._scene_click(i))
            b.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda _p, i=i: self._scene_menu(i))
            sc.addWidget(b)
            self.scene_btns.append(b)
        sc.addStretch(1)
        root.addLayout(sc)

        # --- levý panel
        left = QWidget()
        lv = QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)

        dlbox = QGroupBox("Stáhnout z freesound.org")
        dv = QVBoxLayout(dlbox)
        self.urls = QPlainTextEdit()
        self.urls.setPlaceholderText("URL tabů z freesound.org – jedna na řádek\n"
                                     "např. https://freesound.org/browse/tags/?f=tag:%22short%22\n"
                                     "nebo https://freesound.org/search/?q=techno+kick")
        self.urls.setFixedHeight(90)
        self.urls.textChanged.connect(self._urls_changed)
        dv.addWidget(self.urls)
        form = QFormLayout()
        self.ns_edit = QLineEdit()
        self.ns_edit.setPlaceholderText("auto podle URL")
        form.addRow("Kategorie:", self.ns_edit)
        self.pages = QSpinBox()
        self.pages.setRange(1, 200)
        self.pages.setValue(3)
        self.pages.setToolTip("Kolik stránek výsledků stáhnout (15 zvuků na stránku)")
        form.addRow("Stránek:", self.pages)
        self.maxdur = QSpinBox()
        self.maxdur.setRange(1, 600)
        self.maxdur.setValue(30)
        self.maxdur.setSuffix(" s")
        self.maxdur.setToolTip("Delší zvuky se přeskočí")
        form.addRow("Max. délka:", self.maxdur)
        dv.addLayout(form)
        hb = QHBoxLayout()
        self.dl_btn = mkbtn("⬇ Stáhnout")
        self.dl_btn.clicked.connect(self.start_download)
        self.dl_stop = mkbtn("Zastavit")
        self.dl_stop.setEnabled(False)
        self.dl_stop.clicked.connect(lambda: self.dl and self.dl.stop())
        hb.addWidget(self.dl_btn, 1)
        hb.addWidget(self.dl_stop)
        dv.addLayout(hb)
        self.dl_prog = QProgressBar()
        self.dl_prog.setFormat("%v / %m")
        dv.addWidget(self.dl_prog)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        self.log.setStyleSheet("font-family: monospace; font-size: 11px;")
        dv.addWidget(self.log, 1)
        lv.addWidget(dlbox, 3)

        libbox = QGroupBox("Knihovna – kategorie (zaškrtnuté = v mixeru)")
        lbv = QVBoxLayout(libbox)
        self.lib_list = QListWidget()
        self.lib_list.itemChanged.connect(self._lib_item_changed)
        self.lib_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.lib_list.customContextMenuRequested.connect(self._lib_menu)
        lbv.addWidget(self.lib_list, 1)
        lh = QHBoxLayout()
        b = mkbtn("Obnovit")
        b.clicked.connect(self.rescan)
        lh.addWidget(b)
        b = mkbtn("Importovat soubory…")
        b.clicked.connect(self.import_files)
        lh.addWidget(b)
        b = mkbtn("Složka")
        b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(config.LIBRARY_DIR))))
        lh.addWidget(b)
        lbv.addLayout(lh)
        lv.addWidget(libbox, 2)

        # --- mixer
        self.mix_scroll = QScrollArea()
        self.mix_scroll.setWidgetResizable(True)
        self.mix_inner = QWidget()
        self.mix_lay = QHBoxLayout(self.mix_inner)
        self.mix_lay.setContentsMargins(4, 4, 4, 4)
        self.mix_lay.addStretch(1)
        self.mix_scroll.setWidget(self.mix_inner)
        self.empty_hint = QLabel("Vlevo vlož URL tabu z freesound.org a stáhni zvuky,\n"
                                 "nebo zaškrtni kategorii v knihovně.")
        self.empty_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_hint.setStyleSheet("color: #888; font-size: 16px;")
        self.mix_lay.insertWidget(0, self.empty_hint, 1)

        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.mix_scroll)
        split.setStretchFactor(1, 1)
        split.setSizes([340, 1160])
        root.addWidget(split, 1)
        self.setCentralWidget(central)

        QShortcut(QKeySequence(Qt.Key.Key_Space), self, lambda: self.play_btn.toggle())
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.all_off)
        for i in range(1, N_SCENES + 1):
            QShortcut(QKeySequence(str(i)), self, lambda i=i: self.recall_scene(i))

    def _build_menu(self):
        m = self.menuBar().addMenu("&Soubor")
        m.addAction("Otevřít session…", self._open_session)
        m.addAction("Uložit session jako…", self._save_session_as)
        m.addSeparator()
        m.addAction("Export…", self.export)
        m.addAction("Otevřít složku exportů",
                    lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(config.EXPORT_DIR))))
        m.addSeparator()
        a = QAction("Konec", self)
        a.triggered.connect(self.close)
        m.addAction(a)

    # ------------------------------------------------------------------ knihovna
    def rescan(self):
        self.lib = library.scan_library()
        for ns, sounds in self.lib.items():
            for s in sounds:
                self.engine.add_sound(s)
        self._refresh_lib_list()
        for ns, col in self.columns.items():
            col.set_sounds(self.lib.get(ns, []))

    def _refresh_lib_list(self):
        self.lib_list.blockSignals(True)
        self.lib_list.clear()
        for ns, sounds in self.lib.items():
            it = QListWidgetItem(f"{ns}  ({len(sounds)})")
            it.setData(Qt.ItemDataRole.UserRole, ns)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if ns in self.columns else Qt.CheckState.Unchecked)
            self.lib_list.addItem(it)
        self.lib_list.blockSignals(False)

    def _lib_item_changed(self, it: QListWidgetItem):
        self.set_column_open(it.data(Qt.ItemDataRole.UserRole), it.checkState() == Qt.CheckState.Checked)

    def _lib_menu(self, pos):
        it = self.lib_list.itemAt(pos)
        if not it:
            return
        ns = it.data(Qt.ItemDataRole.UserRole)
        m = QMenu(self)
        m.addAction("Otevřít složku", lambda: QDesktopServices.openUrl(
            QUrl.fromLocalFile(str(config.LIBRARY_DIR / ns))))
        m.addAction("Přejmenovat…", lambda: self.rename_namespace(ns))
        m.addAction("Smazat kategorii…", lambda: self.delete_namespace(ns))
        m.exec(self.lib_list.mapToGlobal(pos))

    def set_column_open(self, ns: str, open_: bool):
        if open_ and ns not in self.columns and ns in self.lib:
            col = GroupColumn(self, ns)
            self.columns[ns] = col
            self.mix_lay.insertWidget(self.mix_lay.count() - 1, col)
        elif not open_ and ns in self.columns:
            self.columns.pop(ns).deleteLater()
        self.empty_hint.setVisible(not self.columns)
        self._refresh_lib_list()

    def rename_namespace(self, ns):
        new, ok = QInputDialog.getText(self, "Přejmenovat kategorii", "Nový název:", text=ns)
        new = library.safe_namespace(new) if ok else ""
        if not new or new == ns or (config.LIBRARY_DIR / new).exists():
            return
        was_open = ns in self.columns
        self.set_column_open(ns, False)
        for s in self.lib.get(ns, []):
            self.engine.remove_track(s.key)
        (config.LIBRARY_DIR / ns).rename(config.LIBRARY_DIR / new)
        self.rescan()
        self.set_column_open(new, was_open)

    def delete_namespace(self, ns):
        n = len(self.lib.get(ns, []))
        if QMessageBox.question(self, "Smazat kategorii",
                                f"Opravdu smazat kategorii „{ns}“ a {n} souborů z disku?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.set_column_open(ns, False)
        for s in self.lib.get(ns, []):
            self.engine.remove_track(s.key)
        shutil.rmtree(config.LIBRARY_DIR / ns, ignore_errors=True)
        self.rescan()

    def copy_to(self, s: Sound, ns: str | None):
        if ns is None:
            ns, ok = QInputDialog.getText(self, "Nová kategorie", "Název kategorie:")
            ns = library.safe_namespace(ns) if ok and ns.strip() else ""
            if not ns:
                return
        if library.copy_sound(s, ns):
            self.rescan()
            self.statusBar().showMessage(f"„{s.title}“ zkopírováno do {ns}", 4000)

    def delete_sound(self, t: Track):
        if QMessageBox.question(self, "Smazat", f"Smazat „{t.sound.title}“ z disku?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.engine.remove_track(t.key)
        library.remove_sound(t.sound)
        self.rescan()

    def import_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Importovat zvuky", str(Path.home()),
                                                "Audio (*.wav *.ogg *.mp3 *.flac *.aif *.aiff *.opus)")
        if not files:
            return
        ns, ok = QInputDialog.getItem(self, "Kategorie", "Do které kategorie:",
                                      list(self.lib) or ["moje"], 0, True)
        ns = library.safe_namespace(ns) if ok else ""
        if ns:
            n = library.import_files(files, ns)
            self.rescan()
            self.set_column_open(ns, True)
            self.statusBar().showMessage(f"Importováno {n} souborů do {ns}", 4000)

    # ------------------------------------------------------------------ stahování
    def _urls_changed(self):
        lines = [l.strip() for l in self.urls.toPlainText().splitlines() if l.strip()]
        self.ns_edit.setEnabled(len(lines) <= 1)
        if len(lines) == 1:
            self.ns_edit.setPlaceholderText(scraper.suggest_namespace(lines[0]) or "auto podle URL")
        else:
            self.ns_edit.setPlaceholderText("auto podle URL (každá URL zvlášť)")

    def start_download(self):
        lines = [l.strip() for l in self.urls.toPlainText().splitlines() if l.strip()]
        lines = [l for l in lines if "freesound.org" in l]
        if not lines:
            QMessageBox.information(self, "Stáhnout", "Vlož aspoň jednu URL z freesound.org.")
            return
        jobs = []
        for url in lines:
            ns = self.ns_edit.text().strip() if len(lines) == 1 else ""
            ns = library.safe_namespace(ns or scraper.suggest_namespace(url))
            jobs.append((url, ns))
        self.dl = DownloadThread(jobs, self.pages.value(), self.maxdur.value())
        self.dl.log.connect(self.log.appendPlainText)
        self.dl.progress.connect(lambda a, b: (self.dl_prog.setMaximum(b), self.dl_prog.setValue(a)))
        self.dl.ns_done.connect(self._ns_downloaded)
        self.dl.finished.connect(self._download_finished)
        self.dl_btn.setEnabled(False)
        self.dl_stop.setEnabled(True)
        self.dl.start()

    def _ns_downloaded(self, ns):
        self.rescan()
        self.set_column_open(ns, True)

    def _download_finished(self):
        self.dl_btn.setEnabled(True)
        self.dl_stop.setEnabled(False)
        self.dl = None

    # ------------------------------------------------------------------ přehrávání
    def load_track(self, t: Track):
        if t.buffer is not None or t.loading:
            return
        t.loading = True
        key, path = t.key, t.sound.path

        def job():
            try:
                buf = library.load_audio(path)
            except Exception as e:
                buf = e
            self.bridge.loaded.emit(key, buf)

        self.pool.submit(job)

    def _on_loaded(self, key, buf):
        t = self.engine.tracks.get(key)
        if t is None:
            return
        t.loading = False
        if isinstance(buf, Exception):
            t.want = False
            self.log.appendPlainText(f"Nelze načíst {t.sound.file}: {buf}")
        else:
            self.engine.set_buffer(t, buf)
            if t.want:
                t.want = False
                self.engine.request(t, True)
        self.refresh_track(t)

    def toggle(self, t: Track, on: bool):
        if on and t.state is None:
            t.want = True
            self.load_track(t)
        else:
            t.want = False
            self.engine.request(t, on)
        self.refresh_track(t)

    def refresh_track(self, t: Track):
        col = self.columns.get(t.group)
        if col and t.key in col.rows:
            col.rows[t.key].refresh()
            col.update_count()
        if t.pending is not None or t.loading:
            self.watch.add(t.key)

    def set_playing(self, on: bool):
        if on:
            self.engine.play()
        else:
            self.engine.stop()
            if self.rec_btn.isChecked():
                self.rec_btn.setChecked(False)
        self.play_btn.setText("■  Stop" if on else "▶  Play")

    def _bpm_changed(self, v):
        self.engine.set_bpm(v)
        for col in self.columns.values():
            for r in col.rows.values():
                if r.t.buffer is not None:
                    r.refresh()

    def all_off(self):
        self.engine.all_off()
        for col in self.columns.values():
            for r in col.rows.values():
                r.refresh()
            col.update_count()

    def _tick(self):
        e = self.engine
        self.meter.setValue(int(min(1.0, e.peak) * 100))
        fpb = e.frames_per_beat
        beat = int(e.frame // fpb)
        bar, b = divmod(beat, BEATS_PER_BAR)
        dots = "".join("●" if i == b else "○" for i in range(BEATS_PER_BAR))
        self.pos.setText(f"{bar + 1:4d}  {dots}" if e.playing else "   –  ○○○○")
        if self.watch:
            done = set()
            for key in self.watch:
                t = e.tracks.get(key)
                if t is None:
                    done.add(key)
                    continue
                col = self.columns.get(t.group)
                if col and key in col.rows:
                    col.rows[key].refresh()
                    col.update_count()
                if t.pending is None and not t.loading:
                    done.add(key)
            self.watch -= done
        if e.recording:
            s = int(time.time() - self.rec_started)
            self.rec_btn.setText(f"● {s // 60:02d}:{s % 60:02d}")
        if e.last_error:
            self.log.appendPlainText("Chyba v audio vlákně:\n" + e.last_error)
            e.last_error = ""

    # ------------------------------------------------------------------ scény
    def _scene_click(self, i):
        if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ControlModifier:
            self.store_scene(i)
        else:
            self.recall_scene(i)

    def _scene_menu(self, i):
        m = QMenu(self)
        m.addAction("Uložit aktuální stav", lambda: self.store_scene(i))
        m.addAction("Smazat", lambda: (self.scenes.pop(str(i), None), self._refresh_scene_btns()))
        m.exec(self.cursor().pos())

    def store_scene(self, i):
        self.scenes[str(i)] = [t.key for t in self.engine.tracks.values() if t.on]
        self._refresh_scene_btns()
        self.statusBar().showMessage(f"Scéna {i} uložena ({len(self.scenes[str(i)])} zvuků)", 3000)

    def recall_scene(self, i):
        keys = self.scenes.get(str(i))
        if keys is None:
            return
        keys = set(keys)
        at = self.engine.next_boundary()
        for t in self.engine.tracks.values():
            want = t.key in keys
            if t.on == want:
                continue
            if want and t.state is None:
                t.want = True
                self.load_track(t)
            else:
                t.want = False
                self.engine.request(t, want, at)
            self.refresh_track(t)

    def _refresh_scene_btns(self):
        for i, b in enumerate(self.scene_btns, 1):
            b.setProperty("filled", "1" if self.scenes.get(str(i)) else "0")
            repolish(b)

    # ------------------------------------------------------------------ nahrávání / export
    def set_recording(self, on: bool):
        if on:
            config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            path = config.EXPORT_DIR / datetime.now().strftime("rec_%Y%m%d_%H%M%S.wav")
            self.engine.start_recording(path)
            self.rec_started = time.time()
            if not self.play_btn.isChecked():
                self.play_btn.setChecked(True)
            self.statusBar().showMessage(f"Nahrávám do {path}")
        else:
            res = self.engine.stop_recording()
            self.rec_btn.setText("● REC")
            if res:
                path, used = res
                sounds = [self.engine.tracks[k].sound for k in used if k in self.engine.tracks]
                write_credits(path.with_name(path.stem + "_credits.txt"), sounds)
                self.statusBar().showMessage(f"Nahrávka uložena: {path}", 8000)
                self.log.appendPlainText(f"Nahrávka: {path}")

    def export(self):
        tracks = [t for t in self.engine.tracks.values() if t.on]
        if not tracks:
            QMessageBox.information(self, "Export", "Nejsou zapnuté žádné zvuky.")
            return
        dlg = ExportDialog(self, len(tracks), self.engine.bpm)
        if not dlg.exec():
            return
        out = Path(dlg.folder.text()).expanduser()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            files = self.do_export(out, dlg.bars.value(), dlg.mix.isChecked(),
                                   dlg.stems.isChecked(), dlg.loops.isChecked())
        except Exception as ex:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Export", f"Export selhal:\n{ex}")
            return
        QApplication.restoreOverrideCursor()
        self.log.appendPlainText(f"Export do {out}: {len(files)} souborů")
        if QMessageBox.information(self, "Export hotov", f"Uloženo {len(files)} souborů do\n{out}",
                                   QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Ok) \
                == QMessageBox.StandardButton.Open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(out)))

    def do_export(self, out: Path, bars: int, mix=True, stems=True, loops=False) -> list[str]:
        e = self.engine
        tracks = [t for t in e.tracks.values() if t.on]
        for t in tracks:
            if t.state is None:
                e.set_buffer(t, library.load_audio(t.sound.path))
        out.mkdir(parents=True, exist_ok=True)
        frames = e.frames_for_bars(bars)
        files = []
        if mix:
            e.render_offline(out / "mix.wav", frames, tracks)
            files.append("mix.wav")
        if stems:
            for ns in sorted({t.group for t in tracks}):
                if e.groups[ns].mute:
                    continue
                name = f"stem_{library.slug(ns, 50)}.wav"
                e.render_offline(out / name, frames, [t for t in tracks if t.group == ns], limiter=False)
                files.append(name)
        if loops:
            for t in tracks:
                name = f"loops/{library.slug(t.group, 20)}__{library.slug(t.sound.title, 50)}.wav"
                e.render_offline(out / name, t.state[1], [t], respect_groups=False,
                                 limiter=False, master=1.0)
                files.append(name)
        write_credits(out / "CREDITS.txt", [t.sound for t in tracks])
        (out / "export.json").write_text(json.dumps(
            {"bpm": e.bpm, "bars": bars, "session": self.session_dict()},
            ensure_ascii=False, indent=1), "utf-8")
        return files

    # ------------------------------------------------------------------ session
    def session_dict(self) -> dict:
        e = self.engine
        tracks = {}
        for t in e.tracks.values():
            d = {}
            if t.on:
                d["enabled"] = True
            if abs(t.volume - 0.8) > 1e-3:
                d["volume"] = round(t.volume, 3)
            if t.pan:
                d["pan"] = t.pan
            if t.length != "auto":
                d["length"] = t.length
            if t.fit != (t.src_bpm is not None):
                d["fit"] = t.fit
            if d:
                tracks[t.key] = d
        return {
            "bpm": e.bpm, "master": e.master, "quantize": e.quantize,
            "columns": list(self.columns),
            "groups": {n: {"volume": g.volume, "mute": g.mute, "solo": g.solo} for n, g in e.groups.items()},
            "tracks": tracks, "scenes": self.scenes,
        }

    def save_session(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.session_dict(), ensure_ascii=False, indent=1), "utf-8")

    def load_session(self, path: Path) -> bool:
        try:
            d = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        e = self.engine
        self.all_off()
        self.bpm.setValue(float(d.get("bpm", 130)))
        self.master.setValue(int(d.get("master", 0.8) * 100))
        q = d.get("quantize", "beat")
        self.quant.setCurrentIndex(next((i for i, (k, _) in enumerate(QUANTIZE) if k == q), 1))
        for n, gd in d.get("groups", {}).items():
            g = e.group(n)
            g.volume = float(gd.get("volume", 0.8))
            g.mute = bool(gd.get("mute"))
            g.solo = bool(gd.get("solo"))
        for key, td in d.get("tracks", {}).items():
            t = e.tracks.get(key)
            if t is None:
                continue
            t.volume = float(td.get("volume", 0.8))
            t.pan = float(td.get("pan", 0.0))
            t.length = td.get("length", "auto") if td.get("length", "auto") in LENGTH_OPTIONS else "auto"
            t.fit = bool(td.get("fit", t.src_bpm is not None))
            if t.buffer is not None:
                e.prepare(t)
            if td.get("enabled"):
                self.toggle(t, True)
        self.scenes = {k: v for k, v in d.get("scenes", {}).items()}
        self._refresh_scene_btns()
        for ns in list(self.columns):
            self.set_column_open(ns, False)
        for ns in d.get("columns", []):
            self.set_column_open(ns, True)
        for col in self.columns.values():
            col.sync_controls()
            for r in col.rows.values():
                r.sync_controls()
            col.update_count()
        return True

    def _open_session(self):
        p, _ = QFileDialog.getOpenFileName(self, "Otevřít session", str(config.HOME), "Session (*.json)")
        if p:
            self.load_session(Path(p))

    def _save_session_as(self):
        p, _ = QFileDialog.getSaveFileName(self, "Uložit session", str(config.HOME / "moje_session.json"),
                                           "Session (*.json)")
        if p:
            self.save_session(Path(p))

    def closeEvent(self, ev):
        if self.dl:
            self.dl.stop()
            self.dl.wait(3000)
        if self.engine.recording:
            self.rec_btn.setChecked(False)
        self.save_session(config.SESSION_FILE)
        self.engine.playing = False
        self.output.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(ev)


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


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Novotného Sound Studio")
    dark_palette(app)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

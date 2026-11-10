"""Hlavní (jediné) okno: transport nahoře, záložky Mix a Postprodukce."""
from __future__ import annotations

import json
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QByteArray, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMainWindow, QMenu,
    QMessageBox, QPlainTextEdit, QProgressBar, QSpinBox, QSplitter, QTabWidget, QVBoxLayout,
    QWidget,
)

from . import config, library, scraper
from .arrangement import Recorder
from .audio_out import create_output
from .engine import BEATS_PER_BAR, LENGTH_OPTIONS, Engine
from .gui_common import AudioService, dark_palette, mkbtn, mkslider, nofocus, set_checked, write_credits
from .gui_mixer import MixerPanel
from .gui_timeline import PostPanel
from .library import Sound

QUANTIZE = [("off", "Okamžitě"), ("beat", "Na dobu"), ("bar", "Na takt")]
TAB_MIX, TAB_POST = 0, 1


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


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Novotného Sound Studio")
        self.resize(1500, 900)
        self.engine = Engine()
        self.deck = self.engine.new_deck("Mix")      # jeden mixer
        self.output = create_output(self.engine)
        self.svc = AudioService(self.engine)
        self.svc.failed.connect(lambda k, err: self.log.appendPlainText(f"Nelze načíst {k}: {err}"))
        self.svc.loaded.connect(lambda key: self.mixer.refresh_rows(key))
        self.lib: dict[str, list[Sound]] = {}
        self.sounds: dict[str, Sound] = {}
        self.dl: DownloadThread | None = None
        self.rec_started = 0.0
        self.break_beat = 0.0

        self._build_ui()
        self.ext = None
        self._extensions_menu()
        self.rescan()
        if not self.load_session(config.SESSION_FILE):
            for ns in list(self.lib)[:4]:
                self.set_column_open(ns, True)
        self.post.refresh()
        self.output.start()
        self.statusBar().showMessage(f"Zvukový výstup: {self.output.name}  ·  knihovna: {config.LIBRARY_DIR}")

        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(50)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(8, 8, 8, 4)

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
        self.quant.setCurrentIndex(2)
        self.quant.setToolTip("Kdy se zapnutí/vypnutí zvuku projeví")
        self.quant.currentIndexChanged.connect(lambda i: setattr(self.engine, "quantize", self.quant.itemData(i)))
        self.pos = QLabel()
        self.pos.setMinimumWidth(130)
        self.pos.setStyleSheet("font-family: monospace; font-size: 15px;")
        self.master = mkslider(int(self.engine.master * 100), tip="Master hlasitost")
        self.master.setMinimumWidth(70)
        self.master.setMaximumWidth(150)
        self.master.valueChanged.connect(lambda v: setattr(self.engine, "master", v / 100))
        self.meter = QProgressBar()
        self.meter.setRange(0, 100)
        self.meter.setTextVisible(False)
        self.meter.setFixedHeight(12)
        self.meter.setMinimumWidth(50)
        self.meter.setMaximumWidth(130)
        self.active = QLabel()
        self.active.setToolTip("Kolik zvuků je zapnutých")
        self.max_spin = nofocus(QSpinBox())
        self.max_spin.setRange(0, 32)
        self.max_spin.setSpecialValueText("max ∞")
        self.max_spin.setPrefix("max ")
        self.max_spin.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.max_spin.setToolTip("Kolik zvuků smí hrát najednou. Když zapneš další,\n"
                                 "nejstarší se na takt vypne (∞ = bez limitu).\n"
                                 "Snížení limitu rovnou proředí mix.")
        self.max_spin.valueChanged.connect(self._max_changed)
        self.break_btn = mkbtn("Break", True, "brk",
                               "Break (B): na takt vypne všechno a zapamatuje si, co hrálo.\n"
                               "Během breaku můžeš pustit pár zvuků (riser, pad…).\n"
                               "Drop (B znovu): na takt vrátí původní mix.")
        self.break_btn.setMinimumWidth(110)
        self.break_btn.toggled.connect(self.set_break)
        panic = mkbtn("Vše vypnout", tip="Vypnout všechny zvuky (Esc)")
        panic.clicked.connect(self.all_off)
        self.rec_btn = mkbtn("● REC", True, "rec",
                             "Nahrávat – po zastavení se nahrávka otevře v Postprodukci")
        self.rec_btn.toggled.connect(self.set_recording)
        for w in (self.play_btn, self.bpm, self.quant, self.pos):
            tr.addWidget(w)
        tr.addSpacing(10)
        tr.addWidget(self.active)
        tr.addWidget(self.max_spin)
        tr.addWidget(self.break_btn)
        tr.addSpacing(10)
        tr.addWidget(QLabel("Master"))
        tr.addWidget(self.master)
        tr.addWidget(self.meter)
        tr.addStretch(1)
        tr.addWidget(panic)
        tr.addWidget(self.rec_btn)
        root.addLayout(tr)

        # --- záložka Mix: vlevo stahování + knihovna, vpravo sloupce
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

        libbox = QGroupBox("Knihovna – kategorie (zaškrtnuté = v mixu)")
        lbv = QVBoxLayout(libbox)
        self.lib_list = QListWidget()
        self.lib_list.itemChanged.connect(self._lib_item_changed)
        self.lib_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.lib_list.customContextMenuRequested.connect(self._lib_menu)
        lbv.addWidget(self.lib_list, 1)
        lh = QHBoxLayout()
        for text, fn in (("Obnovit", self.rescan), ("Importovat soubory…", self.import_files),
                         ("Složka", lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(config.LIBRARY_DIR))))):
            b = mkbtn(text)
            b.clicked.connect(fn)
            lh.addWidget(b)
        lbv.addLayout(lh)
        lv.addWidget(libbox, 2)

        self.mixer = MixerPanel(self)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.mixer)
        split.setStretchFactor(1, 1)
        split.setSizes([340, 1160])

        self.post = PostPanel(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(split, "Mix")
        self.tabs.addTab(self.post, "Postprodukce")
        self.tabs.currentChanged.connect(self._tab_changed)
        root.addWidget(self.tabs, 1)
        self.setCentralWidget(central)

        QShortcut(QKeySequence(Qt.Key.Key_Space), self, self._space)
        QShortcut(QKeySequence(Qt.Key.Key_Escape), self, self.all_off)
        QShortcut(QKeySequence(Qt.Key.Key_B), self, self._break_key)

        m = self.menuBar().addMenu("&Soubor")
        m.addAction("Otevřít session…", self._open_session)
        m.addAction("Uložit session jako…", self._save_session_as)
        m.addSeparator()
        m.addAction("Otevřít složku exportů",
                    lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(config.EXPORT_DIR))))
        m.addSeparator()
        m.addAction("Konec", self.close)

    # ------------------------------------------------------------------ rozšíření
    def _extensions_menu(self):
        """Volitelná rozšíření (složka studio/experimental). Když v buildu chybí, menu se nepřidá."""
        try:
            from . import experimental
        except ImportError:
            return
        m = self.menuBar().addMenu("&Nastavení")
        a = m.addAction("Experimentální vzorce (záložka Vzorce)")
        a.setCheckable(True)
        a.setChecked(experimental.enabled())
        a.toggled.connect(lambda on: (experimental.set_enabled(on), self._set_extension(on)))
        self._set_extension(a.isChecked())

    def _set_extension(self, on: bool):
        from . import experimental
        if on and self.ext is None:
            self.ext = experimental.install(self)
        elif not on and self.ext is not None:
            self.ext.remove()
            self.ext = None

    def _space(self):
        if self.tabs.currentIndex() == TAB_POST:
            self.post.toggle_play()
        else:
            self.play_btn.toggle()

    def _break_key(self):
        if self.tabs.currentIndex() == TAB_MIX:
            self.break_btn.toggle()

    def _tab_changed(self, i):
        if i == TAB_POST:
            self.post.refresh()
            if self.post.editor is None:
                self.post.open()

    # ------------------------------------------------------------------ knihovna
    def rescan(self):
        self.lib = library.scan_library()
        self.sounds = {s.key: s for ss in self.lib.values() for s in ss}
        self.mixer.library_changed()
        self._refresh_lib_list()

    def _refresh_lib_list(self):
        self.lib_list.blockSignals(True)
        self.lib_list.clear()
        for ns, sounds in self.lib.items():
            it = QListWidgetItem(f"{ns}  ({len(sounds)})")
            it.setData(Qt.ItemDataRole.UserRole, ns)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked if ns in self.mixer.columns else Qt.CheckState.Unchecked)
            self.lib_list.addItem(it)
        self.lib_list.blockSignals(False)

    def _lib_item_changed(self, it: QListWidgetItem):
        self.set_column_open(it.data(Qt.ItemDataRole.UserRole), it.checkState() == Qt.CheckState.Checked)

    def set_column_open(self, ns: str, open_: bool):
        if open_:
            self.mixer.open_column(ns)
        else:
            self.mixer.close_column(ns)
        self._refresh_lib_list()

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

    def _drop_ns(self, ns: str):
        self.mixer.close_column(ns)
        for s in self.lib.get(ns, []):
            self.engine.drop_sound(s.key)
        self.deck.groups.pop(ns, None)
        self.deck._groups_t = tuple(self.deck.groups.values())

    def rename_namespace(self, ns):
        new, ok = QInputDialog.getText(self, "Přejmenovat kategorii", "Nový název:", text=ns)
        new = library.safe_namespace(new) if ok else ""
        if not new or new == ns or (config.LIBRARY_DIR / new).exists():
            return
        was_open = ns in self.mixer.columns
        self._drop_ns(ns)
        (config.LIBRARY_DIR / ns).rename(config.LIBRARY_DIR / new)
        self.rescan()
        self.set_column_open(new, was_open)

    def delete_namespace(self, ns):
        n = len(self.lib.get(ns, []))
        if QMessageBox.question(self, "Smazat kategorii",
                                f"Opravdu smazat kategorii „{ns}“ a {n} souborů z disku?") \
                != QMessageBox.StandardButton.Yes:
            return
        self._drop_ns(ns)
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

    def delete_sound(self, s: Sound):
        if QMessageBox.question(self, "Smazat", f"Smazat „{s.title}“ z disku?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.engine.drop_sound(s.key)
        library.remove_sound(s)
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
            jobs.append((url, library.safe_namespace(ns or scraper.suggest_namespace(url))))
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

    # ------------------------------------------------------------------ transport
    def set_playing(self, on: bool):
        e = self.engine
        if on:
            self.post.stop()
            if not e.playing:
                e.start_deck(self.deck)
                self.break_beat = 0.0
        else:
            if self.rec_btn.isChecked():
                self.rec_btn.setChecked(False)
            e.stop_all()
        self.play_btn.setText("■  Stop" if on else "▶  Play")

    def stop_live(self):
        if self.engine.playing:
            self.play_btn.setChecked(False)

    def _bpm_changed(self, v):
        self.engine.set_bpm(v)
        for c in self.mixer.columns.values():
            for r in c.rows.values():
                if r.t.state is not None:
                    r.refresh()

    def all_off(self):
        self.engine.all_off()
        self.mixer.refresh_rows()

    def _max_changed(self, v):
        self.engine.max_tracks = v
        self.mixer.enforce_limits()

    def set_break(self, on: bool):
        e = self.engine
        at = e.deck_boundary(self.deck)
        self.break_beat = (at if at >= 0 else e.frame) / e.fpb
        self.mixer.set_break(on)

    def _break_text(self) -> str:
        if not self.break_btn.isChecked():
            return "Break"
        e = self.engine
        if not e.playing:
            return "Drop"
        bars = (e.frame / e.fpb - self.break_beat) / BEATS_PER_BAR
        return "Drop  · čeká" if bars < 0 else f"Drop  · {int(bars) + 1} t."

    def _tick(self):
        e = self.engine
        self.meter.setValue(int(min(1.0, e.peak) * 100))
        self.active.setText(f"hraje {self.mixer.active_count()}")
        self.break_btn.setText(self._break_text())
        if self.play_btn.isChecked() != e.playing:
            set_checked(self.play_btn, e.playing)
            self.play_btn.setText("■  Stop" if e.playing else "▶  Play")
        beat = int(e.frame // e.fpb)
        bar, b = divmod(beat, BEATS_PER_BAR)
        dots = "".join("●" if i == b else "○" for i in range(BEATS_PER_BAR))
        self.pos.setText(f"{bar + 1:4d}  {dots}" if e.playing else "   –  ○○○○")
        for col in self.mixer.columns.values():
            col.update_count()
        if e.recording:
            s = int(time.time() - self.rec_started)
            self.rec_btn.setText(f"● {s // 60:02d}:{s % 60:02d}")
        if e.last_error:
            self.log.appendPlainText("Chyba v audio vlákně:\n" + e.last_error)
            e.last_error = ""

    # ------------------------------------------------------------------ nahrávání
    def set_recording(self, on: bool):
        e = self.engine
        if on:
            config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
            path = config.EXPORT_DIR / datetime.now().strftime("rec_%Y%m%d_%H%M%S.wav")
            if not e.playing:
                self.play_btn.setChecked(True)
            e.start_recording(path, Recorder(e))
            self.rec_started = time.time()
            self.statusBar().showMessage(f"Nahrávám do {path}")
            return
        res = e.stop_recording()
        self.rec_btn.setText("● REC")
        if not res:
            return
        path, used, recorder = res
        write_credits(path.with_name(path.stem + "_credits.txt"),
                      [self.sounds[k] for k in used if k in self.sounds])
        arr = recorder.finish(e)
        arr.audio, arr.name = str(path), path.stem
        if not arr.lanes:
            self.statusBar().showMessage(f"Nahrávka uložena: {path} (nic nehrálo)", 8000)
            return
        apath = path.with_name(path.stem + ".arr.json")
        arr.save(apath)
        self.log.appendPlainText(f"Nahrávka: {path}")
        self.statusBar().showMessage("Nahrávka uložena – je v záložce Postprodukce", 8000)
        self.post.refresh()
        self.post.open(apath)
        self.tabs.setCurrentIndex(TAB_POST)

    def refresh_recordings(self):
        self.post.refresh()

    # ------------------------------------------------------------------ session
    def session_dict(self) -> dict:
        e, d = self.engine, self.deck
        snap = self.mixer.snapshot        # během breaku uložit mix před breakem
        tracks = {}
        for t in d.tracks.values():
            td = {}
            if (t.key in snap) if snap is not None else t.on:
                td["enabled"] = True
            if abs(t.volume - 0.8) > 1e-3:
                td["volume"] = round(t.volume, 3)
            if t.pan:
                td["pan"] = t.pan
            if t.length != "auto":
                td["length"] = t.length
            if t.fit != (t.src_bpm is not None):
                td["fit"] = t.fit
            if td:
                tracks[t.key] = td
        return {
            "version": 3, "bpm": e.bpm, "master": e.master, "quantize": e.quantize,
            "max_tracks": e.max_tracks,
            "geometry": bytes(self.saveGeometry().toHex()).decode(),
            "columns": list(self.mixer.columns),
            "groups": {n: {"volume": g.volume, "mute": g.target("mute"), "solo": g.target("solo"), "one": g.one}
                       for n, g in d.groups.items()},
            "tracks": tracks,
        }

    def save_session(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.session_dict(), ensure_ascii=False, indent=1), "utf-8")

    def load_session(self, path: Path) -> bool:
        try:
            d = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        if d.get("decks"):          # session z verze s decky -> vezme se první deck
            d = {**d, **d["decks"][0]}
        e, deck = self.engine, self.deck
        set_checked(self.break_btn, False)
        self.mixer.snapshot = None
        self.all_off()
        self.bpm.setValue(float(d.get("bpm", 130)))
        self.master.setValue(int(d.get("master", 0.8) * 100))
        q = d.get("quantize", "bar")
        self.quant.setCurrentIndex(next((i for i, (k, _) in enumerate(QUANTIZE) if k == q), 2))
        if d.get("geometry"):
            self.restoreGeometry(QByteArray.fromHex(d["geometry"].encode()))
        for n, gd in d.get("groups", {}).items():
            g = deck.group(n)
            g.volume = float(gd.get("volume", 0.8))
            g.mute, g.solo, g.one = bool(gd.get("mute")), bool(gd.get("solo")), bool(gd.get("one"))
            g.pending.clear()
        for key, td in d.get("tracks", {}).items():
            s = self.sounds.get(key)
            if s is None:
                continue
            t = e.track(deck, s)
            t.volume = float(td.get("volume", 0.8))
            t.pan = float(td.get("pan", 0.0))
            ln = td.get("length", "auto")
            t.length = ln if ln in LENGTH_OPTIONS else "auto"
            t.fit = bool(td.get("fit", t.src_bpm is not None))
            e.prepare(t)
            if td.get("enabled"):
                if t.state is not None:
                    t.enabled = True
                else:
                    t.want = True
                    self.svc.ensure_loaded(s)
        for ns in list(self.mixer.columns):
            self.mixer.close_column(ns)
        for ns in d.get("columns", []):
            self.mixer.open_column(ns)
        self.max_spin.blockSignals(True)
        self.max_spin.setValue(int(d.get("max_tracks", 0)))
        self.max_spin.blockSignals(False)
        e.max_tracks = self.max_spin.value()
        self.mixer.sync_controls()
        self._refresh_lib_list()
        return True

    def _open_session(self):
        p, _ = QFileDialog.getOpenFileName(self, "Otevřít session", str(config.HOME), "Session (*.json)")
        if p:
            self.stop_live()
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
        self.post.close_editor()
        if self.ext is not None:
            self.ext.panel.shutdown()
        self.save_session(config.SESSION_FILE)
        self.engine.playing = False
        self.output.stop()
        self.svc.shutdown()
        super().closeEvent(ev)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Novotného Sound Studio")
    dark_palette(app)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

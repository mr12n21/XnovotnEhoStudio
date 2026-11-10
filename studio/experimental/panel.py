"""Záložka „Vzorce“ – experimentální rozšíření. Jádra aplikace se nedotýká:
používá jen to, co jde udělat i rukou (soubor v knihovně, zapnutí zvuku v mixu,
úprava v postprodukci přes Zpět/Znovu)."""
from __future__ import annotations

import json
from typing import TYPE_CHECKING

import numpy as np
import soundfile as sf
from PySide6.QtCore import QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QSizePolicy, QSpinBox,
    QTabWidget, QVBoxLayout, QWidget,
)

from .. import config, library
from ..engine import BEATS_PER_BAR
from ..gui_common import mkbtn
from ..waveform import compute_peaks
from . import generators as gen
from .formula import Formula, FormulaError

if TYPE_CHECKING:
    from ..gui import MainWindow

NS = "vzorce"                       # kategorie pro vygenerované zvuky
STATE_FILE = "experimental.json"    # naposledy použité vzorce
HELP_FUNCS = ("funkce: sin cos tan exp log sqrt abs floor ceil round min max clip where "
              "tri saw square (perioda 1) · konstanty pi e tau · "
              "a and/or/not, x if podmínka else y, % // ** & | ^ >> <<")


# ====================================================================== pomocné widgety

class FormulaBox(QWidget):
    """Řádek se vzorcem + příklady + hláška o chybě."""

    def __init__(self, examples, names: set[str], extra: set[str], help_vars: str, on_change=None):
        super().__init__()
        self.names, self.extra, self.on_change = names, extra, on_change
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        top = QHBoxLayout()
        self.edit = QLineEdit()
        self.edit.setStyleSheet("font-family: monospace; font-size: 14px;")
        self.edit.setPlaceholderText("vzorec…")
        self.ex = QComboBox()
        self.ex.addItem("Příklady…")
        self.examples = examples
        for item in examples:
            self.ex.addItem(item[0])
        self.ex.activated.connect(self._example)
        top.addWidget(self.edit, 1)
        top.addWidget(self.ex)
        v.addLayout(top)
        self.err = QLabel()
        self.err.setWordWrap(True)
        v.addWidget(self.err)
        hl = QLabel(f"proměnné: {help_vars}\n{HELP_FUNCS}")
        hl.setWordWrap(True)
        hl.setStyleSheet("color: #7b838c; font-size: 11px;")
        v.addWidget(hl)
        self.edit.textChanged.connect(self._changed)

    def _example(self, i):
        if i > 0:
            self.edit.setText(self.examples[i - 1][1])
            self.ex.setCurrentIndex(0)
            self.example_chosen(self.examples[i - 1])

    def example_chosen(self, item):
        pass

    def text(self) -> str:
        return self.edit.text()

    def set_error(self, msg: str | None):
        if msg:
            self.err.setText(msg)
            self.err.setStyleSheet("color: #ff6b6b;")
        else:
            self.err.setText("✓ vzorec je v pořádku")
            self.err.setStyleSheet("color: #5ec88a;")

    def _changed(self):
        try:
            Formula(self.text(), self.names, self.extra)
            self.set_error(None)
        except FormulaError as e:
            self.set_error(str(e))
        if self.on_change:
            self.on_change()


class PatternView(QWidget):
    """Náhled rytmu: sloupeček = krok, výška = hlasitost úderu."""

    def __init__(self):
        super().__init__()
        self.pattern = np.zeros(0)
        self.steps = 16
        self.setMinimumHeight(70)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set(self, pattern, steps):
        self.pattern, self.steps = pattern, steps
        self.update()

    def paintEvent(self, ev):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#111315"))
        n = len(self.pattern)
        if not n:
            return
        w = self.width() / n
        h = self.height() - 6
        for i, g in enumerate(self.pattern):
            x = i * w
            bar_start = i % self.steps == 0
            beat = i % max(1, self.steps // 4) == 0
            p.fillRect(QRectF(x, 0, max(1.0, w - 1), self.height()),
                       QColor("#262a30" if bar_start else "#1d2024" if beat else "#17191c"))
            if g > 0:
                hh = h * min(1.0, g)
                p.fillRect(QRectF(x + 1, self.height() - 3 - hh, max(1.0, w - 3), hh), QColor("#5ec8ff"))


# ====================================================================== záložka

class FormulaPanel(QWidget):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.state = self._load_state()
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        intro = QLabel("Experimentální rozšíření: hudba podle vzorců. Výsledky jsou obyčejné zvuky v kategorii "
                       f"„{NS}“ a obyčejné křivky v postprodukci – jde s nimi dál pracovat jako se vším ostatním.")
        intro.setWordWrap(True)
        intro.setStyleSheet("color: #8a929b;")
        v.addWidget(intro)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._rhythm_tab(), "Rytmus")
        self.tabs.addTab(self._synth_tab(), "Zvuk")
        self.tabs.addTab(self._curve_tab(), "Křivka")
        self.tabs.addTab(self._autopilot_tab(), "Autopilot")
        v.addWidget(self.tabs, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        v.addWidget(self.status)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._autopilot_tick)
        self.timer.start(30)

    # ------------------------------------------------------------ stav
    def _load_state(self) -> dict:
        try:
            return json.loads((config.HOME / STATE_FILE).read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_state(self):
        self.state.update({"rhythm": self.r_f.text(), "synth": self.s_f.text(), "curve": self.c_f.text(),
                           "autopilot": self.a_f.text()})
        try:
            (config.HOME / STATE_FILE).write_text(json.dumps(self.state, ensure_ascii=False, indent=1), "utf-8")
        except OSError:
            pass

    def say(self, msg: str, ok: bool = True):
        self.status.setText(msg)
        self.status.setStyleSheet(f"color: {'#5ec88a' if ok else '#ff6b6b'};")

    # ------------------------------------------------------------ publikace do knihovny
    def publish(self, name: str, buf: np.ndarray, source: str, turn_on: bool = True):
        """Uloží zvuk do kategorie vzorce (přepíše stejnojmenný) a zapne ho v mixu."""
        main, e = self.main, self.main.engine
        base = library.slug(name, 50)
        uid = f"vzorec:{base}"
        key = f"{NS}/{uid}"
        d = library.namespace_dir(NS)
        sf.write(str(d / f"{base}.wav"), buf, e.sr, subtype="PCM_24")
        meta = library.load_meta(NS)
        meta[uid] = {"title": f"{name} ({e.bpm:g} bpm)", "file": f"{base}.wav", "source": source}
        library.save_meta(NS, meta)
        main.rescan()
        main.set_column_open(NS, True)
        s = main.sounds.get(key)
        if s is None:
            return
        main.svc.peaks.put(s, compute_peaks(buf))
        t = e.track(main.deck, s)
        if key in e.buffers:          # už hraje -> vyměnit audio za běhu
            e.set_buffer(key, library.load_audio(s.path))
        t.src_bpm = e.bpm             # napasovat do tempa i po změně BPM
        e.set_track_params(t, fit=True)
        main.mixer.refresh_rows(key)
        if turn_on and not t.on:
            main.mixer.toggle(t, True)
        self.say(f"„{name}“ je v Mixu v kategorii {NS}" + ("" if e.playing else " – pusť Play (mezerník)"))

    # ------------------------------------------------------------ Rytmus
    def _rhythm_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.r_src = QComboBox()
        self.r_src.setToolTip("Zvuk, ze kterého se rytmus složí (jeden úder = celý zvuk)")
        self.r_src.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.r_src.setMinimumContentsLength(30)
        self.r_bars = QSpinBox()
        self.r_bars.setRange(1, 16)
        self.r_bars.setValue(2)
        self.r_bars.setSuffix(" takt(y)")
        self.r_steps = QComboBox()
        for s in (8, 12, 16, 24, 32):
            self.r_steps.addItem(f"{s} kroků na takt", s)
        self.r_steps.setCurrentIndex(2)
        self.r_seed = QSpinBox()
        self.r_seed.setRange(0, 9999)
        self.r_seed.setToolTip("Číslo náhody – stejné číslo = stejný „náhodný“ výsledek")
        self.r_choke = QCheckBox("Další úder usekne předchozí")
        self.r_name = QLineEdit(self.state.get("rhythm_name", "rytmus"))
        self.r_view = PatternView()
        self.r_f = FormulaBox(gen.RHYTHM_EXAMPLES, gen.RHYTHM_VARS, {"euklid", "nahoda"},
                              "n = krok od začátku, krok = krok v taktu (0…), takt, doba (0–3), kroku = kroků "
                              "na takt · euklid(k, m, posun) · nahoda() = 0…1 · výsledek: ano/ne nebo hlasitost "
                              "0–2", self._rhythm_preview)
        for wdg in (self.r_bars, self.r_seed):
            wdg.valueChanged.connect(self._rhythm_preview)
        self.r_steps.currentIndexChanged.connect(self._rhythm_preview)
        apply = mkbtn("Použít → do Mixu", tip="Uloží smyčku do kategorie „vzorce“ a zapne ji")
        apply.clicked.connect(self._rhythm_apply)
        f.addRow("Zvuk:", self.r_src)
        f.addRow("Vzorec:", self.r_f)
        f.addRow("Náhled:", self.r_view)
        row = QHBoxLayout()
        for wdg in (self.r_bars, self.r_steps, QLabel("náhoda:"), self.r_seed, self.r_choke):
            row.addWidget(wdg)
        row.addStretch(1)
        f.addRow("Délka:", row)
        row2 = QHBoxLayout()
        row2.addWidget(self.r_name, 1)
        row2.addWidget(apply)
        f.addRow("Název:", row2)
        self.r_f.edit.setText(self.state.get("rhythm", "euklid(5, 16)"))
        return w

    def _fill_sources(self):
        cur = self.r_src.currentData()
        self.r_src.blockSignals(True)
        self.r_src.clear()
        for ns, sounds in self.main.lib.items():
            if ns == NS:
                continue
            for s in sounds:
                self.r_src.addItem(f"{ns} / {s.title}", s.key)
        i = self.r_src.findData(cur or self.state.get("rhythm_src"))
        self.r_src.setCurrentIndex(max(0, i))
        self.r_src.blockSignals(False)

    def _rhythm_pattern(self):
        steps = self.r_steps.currentData()
        return gen.rhythm_pattern(self.r_f.text(), self.r_bars.value(), steps, self.main.engine.bpm,
                                  self.r_seed.value()), steps

    def _rhythm_preview(self, *_):
        try:
            pat, steps = self._rhythm_pattern()
        except FormulaError:
            pat, steps = np.zeros(0), 16
        self.r_view.set(pat, steps)

    def _rhythm_apply(self):
        key = self.r_src.currentData()
        s = self.main.sounds.get(key) if key else None
        if s is None:
            self.say("Vyber zvuk (nejdřív nějaký stáhni nebo importuj).", False)
            return
        try:
            pat, steps = self._rhythm_pattern()
            buf = self.main.svc.load_sync(s)
            if buf is None:
                raise ValueError(f"Nejde načíst {s.title}")
            out = gen.rhythm(buf, pat, self.r_bars.value(), self.main.engine.bpm, self.main.engine.sr,
                             self.r_choke.isChecked())
        except (FormulaError, ValueError) as e:
            self.say(str(e), False)
            return
        if not pat.any():
            self.say("Vzorec nedává žádný úder.", False)
            return
        self.state.update({"rhythm_name": self.r_name.text(), "rhythm_src": key})
        self._save_state()
        self.publish(self.r_name.text().strip() or "rytmus", out, f"rytmus: {self.r_f.text()} ze {s.title}")

    # ------------------------------------------------------------ Zvuk
    def _synth_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.s_mode = QComboBox()
        self.s_mode.addItem("vlna – t v sekundách, výsledek −1…1", "vlna")
        self.s_mode.addItem("bytebeat – t = celé číslo 8000×/s, výsledek & 255", "bytebeat")
        self.s_len = QDoubleSpinBox()
        self.s_len.setRange(0.25, 64)
        self.s_len.setSingleStep(0.25)
        self.s_len.setValue(float(self.state.get("synth_len", 1)))
        self.s_len.setSuffix(" dob")
        self.s_len.setToolTip("Délka zvuku v dobách (při současném tempu) – smyčka pak sedí do taktu")
        self.s_seed = QSpinBox()
        self.s_seed.setRange(0, 9999)
        self.s_name = QLineEdit(self.state.get("synth_name", "zvuk"))
        self.s_f = FormulaBox([(a, b) for a, b, _ in gen.SYNTH_EXAMPLES], gen.SYNTH_VARS, {"nahoda"},
                              "t = čas (s, u bytebeatu celé číslo), k = kanál (0 levý, 1 pravý), doba = délka "
                              "doby v s, bpm, f = vzorkovací frekvence · nahoda() = 0…1")
        self.s_f.example_chosen = lambda item: self.s_mode.setCurrentIndex(
            self.s_mode.findData(dict((a, m) for a, _, m in gen.SYNTH_EXAMPLES).get(item[0], "vlna")))
        self.s_mode.setCurrentIndex(max(0, self.s_mode.findData(self.state.get("synth_mode", "vlna"))))
        apply = mkbtn("Použít → do Mixu", tip="Vyrobí zvuk, uloží ho do kategorie „vzorce“ a zapne ho")
        apply.clicked.connect(self._synth_apply)
        f.addRow("Druh:", self.s_mode)
        f.addRow("Vzorec:", self.s_f)
        row = QHBoxLayout()
        for wdg in (self.s_len, QLabel("náhoda:"), self.s_seed):
            row.addWidget(wdg)
        row.addStretch(1)
        f.addRow("Délka:", row)
        row2 = QHBoxLayout()
        row2.addWidget(self.s_name, 1)
        row2.addWidget(apply)
        f.addRow("Název:", row2)
        self.s_f.edit.setText(self.state.get("synth", gen.SYNTH_EXAMPLES[0][1]))
        return w

    def _synth_apply(self):
        e = self.main.engine
        try:
            out = gen.synth(self.s_f.text(), self.s_mode.currentData(), self.s_len.value() * 60.0 / e.bpm,
                            e.sr, e.bpm, self.s_seed.value())
        except (FormulaError, ValueError) as ex:
            self.say(str(ex), False)
            return
        self.state.update({"synth_name": self.s_name.text(), "synth_mode": self.s_mode.currentData(),
                           "synth_len": self.s_len.value()})
        self._save_state()
        self.publish(self.s_name.text().strip() or "zvuk", out, f"vzorec ({self.s_mode.currentData()}): "
                     f"{self.s_f.text()}")

    # ------------------------------------------------------------ Křivka
    def _curve_tab(self):
        from ..gui_timeline import CURVES
        self._curves = CURVES
        w = QWidget()
        f = QFormLayout(w)
        self.c_target = QComboBox()
        self.c_kind = QComboBox()
        for k, cv in CURVES.items():
            self.c_kind.addItem(f"{cv['label']} (0–{cv['vmax'] * 100:.0f} %)", k)
        self.c_from = QSpinBox()
        self.c_from.setRange(1, 9999)
        self.c_from.setPrefix("od taktu ")
        self.c_to = QSpinBox()
        self.c_to.setRange(1, 9999)
        self.c_to.setValue(9)
        self.c_to.setPrefix("do taktu ")
        self.c_step = QComboBox()
        for label, st in (("bod každou 1/4 doby", 0.25), ("bod každou 1/2 doby", 0.5), ("bod každou dobu", 1.0),
                          ("bod každý takt", 4.0)):
            self.c_step.addItem(label, st)
        self.c_step.setCurrentIndex(1)
        self.c_f = FormulaBox(gen.CURVE_EXAMPLES, gen.CURVE_VARS, set(),
                              "t = doby od začátku výběru, x = 0…1 přes výběr, takt = t/4, delka = délka výběru "
                              "v dobách · výsledek: 1 = 100 % (hlasitost až 2 = 200 %, špičky/šum 0–1)")
        apply = mkbtn("Použít → do Postprodukce",
                      tip="Přepíše křivku ve vybraném rozsahu (Ctrl+Z v Postprodukci vrátí)")
        apply.clicked.connect(self._curve_apply)
        refresh = mkbtn("↻", tip="Načíst stopy z otevřené nahrávky", width=30)
        refresh.clicked.connect(self._fill_targets)
        row0 = QHBoxLayout()
        row0.addWidget(self.c_target, 1)
        row0.addWidget(refresh)
        f.addRow("Stopa:", row0)
        f.addRow("Křivka:", self.c_kind)
        f.addRow("Vzorec:", self.c_f)
        row = QHBoxLayout()
        for wdg in (self.c_from, self.c_to, self.c_step):
            row.addWidget(wdg)
        row.addStretch(1)
        row.addWidget(apply)
        f.addRow("Rozsah:", row)
        self.c_f.edit.setText(self.state.get("curve", gen.CURVE_EXAMPLES[0][1]))
        return w

    def _fill_targets(self):
        ed = self.main.post.editor
        cur = self.c_target.currentData()
        self.c_target.clear()
        if ed is None:
            self.c_target.addItem("(otevři nahrávku v Postprodukci)", None)
            return
        self.c_target.addItem(f"MIX – celá nahrávka „{ed.arr.name or ed.path.stem}“", "__mix__")
        for lane in ed.arr.lanes:
            self.c_target.addItem(f"{lane.ns} / {lane.title}", lane.id)
        self.c_target.setCurrentIndex(max(0, self.c_target.findData(cur)))
        if ed.sel:     # výběr v pravítku postprodukce -> rozsah
            self.c_from.setValue(int(ed.sel[0] // BEATS_PER_BAR) + 1)
            self.c_to.setValue(int(-(-ed.sel[1] // BEATS_PER_BAR)) + 1)

    def _curve_apply(self):
        ed = self.main.post.editor
        tid = self.c_target.currentData()
        if ed is None or tid is None:
            self.say("Nejdřív otevři nahrávku v Postprodukci a klikni na ↻.", False)
            return
        cv = self._curves[self.c_kind.currentData()]
        if tid == "__mix__":
            owner, attr = ed.arr, cv["master"]
        else:
            owner = next((l for l in ed.arr.lanes if l.id == tid), None)
            attr = cv["attr"]
            if owner is None:
                self.say("Stopa už v nahrávce není – klikni na ↻.", False)
                return
        a, b = (self.c_from.value() - 1) * BEATS_PER_BAR, (self.c_to.value() - 1) * BEATS_PER_BAR
        try:
            pts = gen.curve_points(self.c_f.text(), a, b, self.c_step.currentData(), cv["vmax"])
        except (FormulaError, ValueError) as ex:
            self.say(str(ex), False)
            return
        ed.edit(lambda: setattr(owner, attr, gen.merge_curve(getattr(owner, attr), pts, a, b)))
        self._save_state()
        self.say(f"Křivka {cv['what']} nastavena ({len(pts)} bodů). V Postprodukci jde vrátit Ctrl+Z.")

    # ------------------------------------------------------------ Autopilot
    def _autopilot_tab(self):
        w = QWidget()
        f = QFormLayout(w)
        self.a_on = QCheckBox("Zapnout autopilota")
        self.a_on.setToolTip("Při přehrávání v Mixu sám zapíná a vypíná zvuky podle vzorce")
        self.a_on.toggled.connect(self._autopilot_toggled)
        self.a_every = QSpinBox()
        self.a_every.setRange(1, 32)
        self.a_every.setValue(int(self.state.get("autopilot_every", 4)))
        self.a_every.setPrefix("každé ")
        self.a_every.setSuffix(" takty")
        self.a_seed = QSpinBox()
        self.a_seed.setRange(0, 9999)
        self.a_f = FormulaBox(gen.AUTOPILOT_EXAMPLES, gen.AUTOPILOT_VARS, {"pravidlo", "nahoda"},
                              "i = pořadí zvuku (0…), N = počet zvuků, zap = hraje teď (0/1), vlevo/vpravo = "
                              "soused, pocet = kolik hraje, takt · pravidlo(30) = buněčný automat · nahoda() "
                              "· výsledek: > 0 = hrát")
        self.a_view = QLabel()
        self.a_view.setStyleSheet("font-family: monospace; font-size: 15px;")
        self.a_view.setWordWrap(True)
        step = mkbtn("Krok teď", tip="Jeden krok autopilota hned (změna se projeví na takt)")
        step.clicked.connect(lambda: self._autopilot_step(manual=True))
        f.addRow("", self.a_on)
        f.addRow("Vzorec:", self.a_f)
        row = QHBoxLayout()
        for wdg in (self.a_every, QLabel("náhoda:"), self.a_seed, step):
            row.addWidget(wdg)
        row.addStretch(1)
        f.addRow("Kdy:", row)
        f.addRow("Zvuky:", self.a_view)
        hint = QLabel("Autopilot pracuje se zvuky v otevřených sloupcích Mixu (zleva doprava, shora dolů, jen ty, "
                      "co projdou filtrem). Limit „max“ a „1×“ u kategorií platí dál.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: #7b838c; font-size: 11px;")
        f.addRow("", hint)
        self.a_f.edit.setText(self.state.get("autopilot", gen.AUTOPILOT_EXAMPLES[0][1]))
        self._a_done = -1
        self._a_takt = 0
        return w

    def _autopilot_tracks(self):
        return [r.t for col in self.main.mixer.columns.values() for r in col.rows.values() if not r.isHidden()]

    def _autopilot_toggled(self, on):
        e = self.main.engine
        self._a_done = int((e.frame / e.fpb) // (self.a_every.value() * BEATS_PER_BAR)) if e.playing else -1
        self.state["autopilot_every"] = self.a_every.value()
        self._save_state()
        self.say("Autopilot zapnutý – běží při přehrávání v Mixu." if on else "Autopilot vypnutý.")

    def _autopilot_step(self, manual=False):
        tracks = self._autopilot_tracks()
        if not tracks:
            self.say("Autopilot: v Mixu nejsou otevřené žádné zvuky.", False)
            return
        states = np.array([t.on for t in tracks], np.int64)
        try:
            new = gen.autopilot_step(self.a_f.text(), states, self._a_takt, self.a_seed.value())
        except FormulaError as ex:
            self.say(f"Autopilot: {ex}", False)
            self.a_on.setChecked(False)
            return
        mixer = self.main.mixer
        for t, want in zip(tracks, new.tolist()):    # nejdřív vypnout, ať limit nevyhodí nové
            if t.on and not want:
                mixer.toggle(t, False)
        for t, want in zip(tracks, new.tolist()):
            if want and not t.on:
                mixer.toggle(t, True)
        self.a_view.setText("".join("●" if x else "○" for x in new) + f"   (takt {self._a_takt + 1})")
        self._a_takt += self.a_every.value()
        if manual:
            self.say("Autopilot: krok hotový.")

    def _autopilot_tick(self):
        if not self.a_on.isChecked():
            return
        e = self.main.engine
        if not e.playing or (e.arr_player is not None and e.arr_player.playing):
            self._a_done = -1
            return
        period = self.a_every.value() * BEATS_PER_BAR
        beat = e.frame / e.fpb
        nxt = int(beat // period) + 1
        # změnu je potřeba poslat chvíli před hranicí, ať naskočí přesně na ni (kvantizace na takt)
        if nxt * period - beat <= 0.5 and nxt != self._a_done:
            self._a_done = nxt
            self._a_takt = nxt * self.a_every.value()
            self._autopilot_step()

    # ------------------------------------------------------------ viditelnost
    def showEvent(self, ev):
        self._fill_sources()
        self._fill_targets()
        self._rhythm_preview()
        super().showEvent(ev)

    def shutdown(self):
        self.timer.stop()
        self._save_state()

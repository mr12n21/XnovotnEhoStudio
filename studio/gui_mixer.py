"""Mixer – sloupce kategorií, každý zvuk jako vlna (klik = zapnout/vypnout)."""
from __future__ import annotations

import random
from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtCore import QLineF, QRectF, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QMenu, QScrollArea, QSizePolicy, QToolButton,
    QVBoxLayout, QWidget,
)

from .engine import LENGTH_OPTIONS, Track, length_label, loop_beats
from .gui_common import mkbtn, mkslider, nofocus, repolish, set_checked, set_value
from .library import Sound

if TYPE_CHECKING:
    from .gui import MainWindow

# pozadí, okraj, vlna, text
WAVE_COLORS = {
    "": ("#24272b", "#3a3f45", "#5d6874", "#cfd3d8"),
    "on": ("#1d7a3c", "#33c06a", "#b8f5c8", "#ffffff"),
    "idle": ("#1f3a28", "#2f6b44", "#5fa877", "#d8f0e0"),     # zapnutý, ale nehraje se (Stop)
    "pend": ("#8a6a12", "#c9a227", "#f0d890", "#ffffff"),
    "off-pend": ("#5a3a12", "#a5521b", "#c89060", "#ffffff"),
    "load": ("#1d4f8c", "#4f8fdc", "#9cc4f0", "#ffffff"),
}


class WaveToggle(QWidget):
    """Tlačítko zvuku = jeho vlna. Klik zapíná/vypíná, bílá čára = pozice ve smyčce."""

    def __init__(self, row: "TrackRow"):
        super().__init__()
        self.row = row
        self.setMinimumWidth(120)
        self.setFixedHeight(self.WAVE_H + self.TEXT_H + 4)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)

    WAVE_H = 30   # vlna nahoře
    TEXT_H = 20   # název pod ní

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MouseButton.LeftButton:
            self.row.clicked()

    def paintEvent(self, ev):
        row = self.row
        bg, border, wave, text = WAVE_COLORS[row.visual_state()]
        p = QPainter(self)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QColor(border))
        p.setBrush(QColor(bg))
        p.drawRoundedRect(r, 3, 3)
        # pruh s názvem pod vlnou (plné tmavší pozadí => vždy čitelné)
        ty = 2 + self.WAVE_H
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(bg).darker(150))
        p.drawRect(QRectF(1, ty, self.width() - 2, self.height() - ty - 1))
        x0, w = 2, self.width() - 4
        h = self.WAVE_H - 4
        mid = 2 + self.WAVE_H / 2
        peaks = row.peaks()
        if peaks is not None and w > 4:
            content, pfrac = row.loop_fracs()
            cw = max(1, int(w * content))
            usable = max(1, int(len(peaks) * pfrac))
            xs = np.arange(cw)
            vals = peaks[np.minimum(xs * usable // cw, usable - 1)] * (h / 2)
            p.setPen(QColor(wave))
            p.drawLines([QLineF(x0 + x, mid - v, x0 + x, mid + v) for x, v in zip(xs.tolist(), vals.tolist())])
        ph = row.playhead()
        if ph is not None:
            x = x0 + ph * w
            p.setPen(QPen(QColor("#ffffff"), 1.5))
            p.drawLine(QLineF(x, 2, x, ty - 1))
        title = p.fontMetrics().elidedText(row.t.sound.title, Qt.TextElideMode.ElideRight, w - 10)
        p.setPen(QColor(text))
        p.drawText(QRectF(6, ty, w - 6, self.TEXT_H), Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                   title)


class TrackRow(QWidget):
    def __init__(self, mixer: "MixerPanel", t: Track):
        super().__init__()
        self.mixer, self.t = mixer, t
        self._last = None
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(3)
        self.wave = WaveToggle(self)
        self.wave.customContextMenuRequested.connect(self._menu)
        self.len = nofocus(QToolButton())
        self.len.setObjectName("len")
        self.len.setToolTip("Délka smyčky v dobách (a = automaticky, free = přirozená délka)")
        self.len.clicked.connect(self._len_menu)
        self.fit = nofocus(QToolButton())
        self.fit.setObjectName("fit")
        self.fit.setText("⇔")
        self.fit.setCheckable(True)
        self.fit.setToolTip("Napasovat zvuk přesně do tempa (přetáhne ho – mění výšku tónu)")
        self.fit.toggled.connect(lambda on: self._set(fit=on))
        self.vol = mkslider(int(t.volume * 100), width=60, tip="Hlasitost zvuku")
        self.vol.valueChanged.connect(lambda v: setattr(self.t, "volume", v / 100))
        lay.addWidget(self.wave, 1)
        lay.addWidget(self.len)
        lay.addWidget(self.fit)
        lay.addWidget(self.vol)
        self.sync_controls()

    # ------------------------------------------------------------ stav pro kreslení
    def visual_state(self) -> str:
        t = self.t
        if t.want:
            return "load"
        if t.pending is not None:
            return "pend" if t.pending[0] else "off-pend"
        if t.enabled:
            d, e = self.mixer.deck, self.mixer.engine
            if not d.running_at(e.frame):
                return "idle"
            # zapnutý, ale neslyšet (mute/solo kategorie) -> tmavší
            return "on" if e.track_gain(t, d, any(g.solo for g in d._groups_t)) > 0 else "idle"
        return ""

    def peaks(self):
        svc = self.mixer.main.svc
        pk = svc.peaks.get(self.t.sound)
        if pk is None:
            svc.request_peaks(self.t.sound)
        return pk

    def loop_fracs(self) -> tuple[float, float]:
        st = self.t.state
        buf = self.mixer.engine.buffers.get(self.t.key)
        if st is None or buf is None or self.t.fit:
            return 1.0, 1.0
        play, L = st
        return min(len(play), L) / L, min(1.0, L / max(1, len(buf)))

    def playhead(self):
        t, d, e = self.t, self.mixer.deck, self.mixer.engine
        if not t.enabled or t.state is None or not d.running_at(e.frame):
            return None
        L = t.state[1]
        return ((e.frame - d.start) % L) / L

    def tick(self):
        st = (self.visual_state(), self.t.state is not None)
        if st != self._last or st[0] == "on":
            self._last = st
            self.wave.update()

    # ------------------------------------------------------------ ovládání
    def clicked(self):
        self.mixer.toggle(self.t, not self.t.on)

    def sync_controls(self):
        set_value(self.vol, int(self.t.volume * 100))
        set_checked(self.fit, self.t.fit)
        self.refresh()

    def refresh(self):
        t, e = self.t, self.mixer.engine
        buf = e.buffers.get(t.key)
        beats = loop_beats(len(buf), e.sr, e.bpm, t.length, t.fit, t.src_bpm) if buf is not None else None
        self.len.setText(f"a{length_label(beats)}" if t.length == "auto" and beats is not None
                         else length_label(t.length))
        s = t.sound
        info = [f"<b>{s.title}</b>", f"autor: {s.username or '?'}", f"licence: {s.license or '?'}"]
        if buf is not None:
            info.append(f"délka: {len(buf) / e.sr:.2f} s")
        elif s.duration:
            info.append(f"délka: {s.duration:.2f} s")
        if beats is not None:
            info.append(f"smyčka: {length_label(beats)} dob")
        if t.src_bpm:
            info.append(f"tempo v názvu: {t.src_bpm:g} BPM")
        info.append("<i>klik = zapnout/vypnout · pravé tlačítko = další volby</i>")
        self.wave.setToolTip("<br>".join(info))
        self._last = None
        self.wave.update()

    def _set(self, **kw):
        self.mixer.engine.set_track_params(self.t, **kw)
        self.refresh()

    def _len_menu(self):
        m = QMenu(self)
        for opt in LENGTH_OPTIONS:
            a = m.addAction(length_label(opt) + ("" if opt in ("auto", "free") else " dob"))
            a.setCheckable(True)
            a.setChecked(opt == self.t.length)
            a.triggered.connect(lambda _=False, o=opt: self._set(length=o))
        m.exec(self.len.mapToGlobal(self.len.rect().bottomLeft()))

    def _menu(self, pos):
        t, s, main = self.t, self.t.sound, self.mixer.main
        m = QMenu(self)
        if s.url:
            m.addAction("Otevřít na freesound.org", lambda: QDesktopServices.openUrl(QUrl(s.url)))
        pan = m.addMenu("Panorama")
        for label, v in [("Vlevo", -0.7), ("Střed", 0.0), ("Vpravo", 0.7)]:
            act = pan.addAction(label, lambda v=v: setattr(t, "pan", v))
            act.setCheckable(True)
            act.setChecked(abs(t.pan - v) < 0.01)
        cp = m.addMenu("Kopírovat do kategorie")
        for ns in main.lib:
            if ns != s.namespace:
                cp.addAction(ns, lambda ns=ns: main.copy_to(s, ns))
        cp.addSeparator()
        cp.addAction("Nová kategorie…", lambda: main.copy_to(s, None))
        m.addSeparator()
        m.addAction("Smazat z knihovny", lambda: main.delete_sound(s))
        m.exec(self.wave.mapToGlobal(pos))


class GroupColumn(QFrame):
    def __init__(self, mixer: "MixerPanel", ns: str):
        super().__init__()
        self.mixer, self.ns = mixer, ns
        self.g = mixer.deck.group(ns)
        self.rows: dict[str, TrackRow] = {}
        self.setObjectName("column")
        self.setFixedWidth(410)
        v = QVBoxLayout(self)
        v.setContentsMargins(6, 6, 6, 6)
        v.setSpacing(4)

        top = QHBoxLayout()
        name = QLabel(ns)
        name.setObjectName("colname")
        self.count = QLabel()
        close = mkbtn("✕", tip="Zavřít sloupec (zvuky zůstanou v knihovně)", width=26)
        close.clicked.connect(lambda: mixer.main.set_column_open(ns, False))
        top.addWidget(name, 1)
        top.addWidget(self.count)
        top.addWidget(close)
        v.addLayout(top)

        ctl = QHBoxLayout()
        self.mute = mkbtn("M", True, "mute", "Ztlumit kategorii", width=30)
        self.solo = mkbtn("S", True, "solo", "Sólo kategorie", width=30)
        self.mute.toggled.connect(lambda on: self._set_group("mute", on))
        self.solo.toggled.connect(lambda on: self._set_group("solo", on))
        self.one = mkbtn("1×", True, "one", "Jen jeden zvuk z kategorie najednou\n"
                         "(zapnutí dalšího vypne předchozí – např. jeden kick)", width=30)
        self.one.toggled.connect(self._set_one)
        rnd = mkbtn("Náh.", tip="Náhodně zapnout 3 zvuky z kategorie (ostatní vypnout)", width=42)
        rnd.clicked.connect(self.randomize)
        off = mkbtn("Vyp", tip="Vypnout všechny zvuky v kategorii", width=40)
        off.clicked.connect(lambda: [mixer.toggle(r.t, False) for r in self.rows.values() if r.t.on])
        self.vol = mkslider(int(self.g.volume * 100), tip="Hlasitost kategorie")
        self.vol.valueChanged.connect(lambda val: setattr(self.g, "volume", val / 100))
        for w in (self.mute, self.solo, self.one, rnd, off):
            ctl.addWidget(w)
        ctl.addWidget(self.vol, 1)
        v.addLayout(ctl)

        self.filter = QLineEdit()
        self.filter.setPlaceholderText("filtr…")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self.apply_filter)
        v.addWidget(self.filter)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        inner = QWidget()
        self.list = QVBoxLayout(inner)
        self.list.setContentsMargins(0, 0, 4, 0)
        self.list.setSpacing(2)
        self.list.addStretch(1)
        scroll.setWidget(inner)
        v.addWidget(scroll, 1)
        self.sync_controls()
        self.set_sounds(mixer.main.lib.get(ns, []))

    def sync_controls(self):
        set_checked(self.mute, self.g.target("mute"))
        set_checked(self.solo, self.g.target("solo"))
        set_checked(self.one, self.g.one)
        set_value(self.vol, int(self.g.volume * 100))

    def _set_group(self, attr: str, on: bool):
        self.mixer.engine.set_group(self.mixer.deck, self.g, attr, on)
        self.tick()

    def _set_one(self, on: bool):
        self.g.one = on
        self.mixer.enforce_limits()

    def tick(self):
        """M/S čekající na takt mají žlutý okraj."""
        for b, attr in ((self.mute, "mute"), (self.solo, "solo")):
            pend = attr in self.g.pending
            if bool(b.property("pending")) != pend:
                b.setProperty("pending", pend)
                repolish(b)

    def set_sounds(self, sounds: list[Sound]):
        keys = {s.key for s in sounds}
        for k in list(self.rows):
            if k not in keys:
                self.rows.pop(k).deleteLater()
        for i, s in enumerate(sounds):
            row = self.rows.get(s.key)
            if row is None:
                row = TrackRow(self.mixer, self.mixer.engine.track(self.mixer.deck, s))
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
        pick = set(random.sample(visible, min(1 if self.g.one else 3, len(visible))))
        for r in self.rows.values():
            want = r.t in pick
            if r.t.on != want:
                self.mixer.toggle(r.t, want)


class MixerPanel(QScrollArea):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main, self.engine, self.deck = main, main.engine, main.deck
        self.columns: dict[str, GroupColumn] = {}
        self.snapshot: set[str] | None = None   # co hrálo před breakem (None = není break)
        self.setWidgetResizable(True)
        inner = QWidget()
        self.cols = QHBoxLayout(inner)
        self.cols.setContentsMargins(4, 4, 4, 4)
        self.cols.addStretch(1)
        self.hint = QLabel("Vlevo vlož URL tabu z freesound.org a stáhni zvuky,\n"
                           "nebo zaškrtni kategorii v knihovně.")
        self.hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.hint.setStyleSheet("color: #888; font-size: 16px;")
        self.cols.insertWidget(0, self.hint, 1)
        self.setWidget(inner)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(40)

    def tick(self):
        if not self.isVisible():
            return
        for col in self.columns.values():
            col.tick()
            for r in col.rows.values():
                r.tick()

    def toggle(self, t: Track, on: bool, limit: bool = True):
        if on:
            self.engine.mark_on(t)
        if on and t.state is None:
            t.want = True
            self.main.svc.ensure_loaded(t.sound)
        else:
            t.want = False
            self.engine.request(t, on)
        if on and limit:
            self.enforce_limits()
        col = self.columns.get(t.group)
        if col:
            col.update_count()

    def enforce_limits(self):
        """Vypne zvuky nad limit (max. počet / 1× v kategorii) – nejstarší první."""
        for t in self.engine.over_limit(self.deck):
            self.toggle(t, False)

    def active_count(self) -> int:
        return sum(1 for t in self.deck.tracks.values() if t.on)

    def set_break(self, on: bool):
        """Break: na takt vypne všechno a zapamatuje si to. Drop: na takt vrátí, co hrálo."""
        tracks = list(self.deck.tracks.values())
        if on:
            self.snapshot = {t.key for t in tracks if t.on}
            for t in tracks:
                if t.on:
                    self.toggle(t, False)
        else:
            snap = self.snapshot or set()
            self.snapshot = None
            for t in tracks:            # nejdřív vypnout, co přibylo během breaku
                if t.on and t.key not in snap:
                    self.toggle(t, False)
            for t in sorted(tracks, key=lambda t: t.seq):   # zachová pořadí pro limit
                if not t.on and t.key in snap:
                    self.toggle(t, True, limit=False)

    def open_column(self, ns: str):
        if ns in self.columns or ns not in self.main.lib:
            return
        col = GroupColumn(self, ns)
        self.columns[ns] = col
        self.cols.insertWidget(self.cols.count() - 1, col)
        self.hint.setVisible(False)

    def close_column(self, ns: str):
        col = self.columns.pop(ns, None)
        if col:
            col.deleteLater()
        self.hint.setVisible(not self.columns)

    def library_changed(self):
        for ns in list(self.columns):
            if ns not in self.main.lib:
                self.close_column(ns)
            else:
                self.columns[ns].set_sounds(self.main.lib[ns])

    def refresh_rows(self, key: str | None = None):
        for col in self.columns.values():
            for k, r in col.rows.items():
                if key is None or k == key:
                    r.refresh()
            col.update_count()

    def sync_controls(self):
        for col in self.columns.values():
            col.sync_controls()
            for r in col.rows.values():
                r.sync_controls()
            col.update_count()

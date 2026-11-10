"""Postprodukce – časová osa nahrávky.

Nahoře vlna celého mixu (kde jsou dropy), pod ní stopa pro každý zvuk.
Úseky = kdy zvuk hrál. Ovládání myší:
  táhni úsek            posun v čase
  táhni okraj úseku     zkrácení / prodloužení
  dvojklik na prázdno   přidat úsek (zapnout zvuk)
  dvojklik na úsek      smazat úsek (vypnout zvuk)
  táhni v pravítku      výběr času (vyjmout / ztišit)
  Ctrl + kolečko        zoom
Režim „Křivka hlasitosti“ (žluté čáry): klik = bod, táhni = posun bodu,
dvojklik na bod = smazat. Nahoře v MIX je křivka celého mixu.
Stejně se kreslí křivky úprav „Špičky“ (ztlumit, co je výrazně hlasitější než
zbytek) a „Šum“ (ztlumit tiché věci mezi zvuky) – síla 0–100 % v čase.
Ve vlně MIX je tmavě vidět původní vlna, světle výsledek po úpravách.
"""
from __future__ import annotations

import json
import math
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
from PySide6.QtCore import QLineF, QObject, QPointF, QRectF, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDesktopServices, QKeySequence, QPainter, QPen, QPolygonF, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGridLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox,
    QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from . import config, library
from .arrangement import (ENV_MAX, ArrPlayer, Arrangement, Compiled, CompiledLane, Lane, Region, analyze_mix,
                          bars_beats, env_frames, fx_pts, region_frames, render_to_file, with_fx)
from .engine import BEATS_PER_BAR, LENGTH_OPTIONS, bpm_from_title, length_label, pan_gains, prepare_state
from .gui_common import mkbtn, mkslider, nofocus, set_checked, set_value, write_credits
from .waveform import compute_peaks

if TYPE_CHECKING:
    from .gui import MainWindow

LANE_H = 50
HEAD_W = 260
RULER_H = 24
MIX_H = 70
SNAPS = [("Takt", 4.0), ("Doba", 1.0), ("1/2 doby", 0.5), ("1/4 doby", 0.25), ("Volně", 0.0)]
MIX_BUCKET_BEATS = 1 / 16
SONG_NS = "vlastni"           # kategorie pro vlastní zvuky / písničky
SONG_MAX_SECONDS = 30 * 60    # písnička se načte celá (až 30 min)
FADES = [("žádný", 0.0), ("1 doba", 1.0), ("1 takt", 4.0), ("2 takty", 8.0), ("4 takty", 16.0), ("8 taktů", 32.0)]
ENV_COLOR = QColor("#ffd84a")         # křivka hlasitosti stopy
MASTER_ENV_COLOR = QColor("#ff9f43")  # křivka hlasitosti celého mixu
HINT = ("táhni úsek = posun · táhni okraj = zkrátit · dvojklik = přidat/smazat úsek · "
        "táhni v pravítku = výběr času · Ctrl+kolečko = zoom · Shift+kolečko = posun")
HINT_VOL = ("KŘIVKA HLASITOSTI: klik = přidat bod · táhni bod = nahoru hlasitěji / dolů tišeji · "
            "dvojklik na bod = smazat · pravé tlačítko = smazat křivku · nahoře v MIX = hlasitost celku")
HINT_TAME = ("ŠPIČKY: kde je křivka nahoře, ztlumí se místa výrazně hlasitější než zbytek stopy "
             "(dole = nechat) · klik = bod · táhni = síla · dvojklik = smazat · nahoře v MIX = celý mix")
HINT_GATE = ("ŠUM: kde je křivka nahoře, ztlumí se tiché věci mezi zvuky – šum, brum, dozvuky "
             "(dole = nechat) · klik = bod · táhni = síla · dvojklik = smazat · nahoře v MIX = celý mix")

# druhy křivek: atribut na stopě / na nahrávce, neutrální hodnota (= nic), maximum, barvy
CURVES = {
    "vol": dict(attr="env", master="master_env", neutral=1.0, vmax=ENV_MAX, color=ENV_COLOR,
                mcolor=MASTER_ENV_COLOR, label="Hlasitost", what="hlasitosti", hint=HINT_VOL,
                tip="Kreslení hlasitosti myší: klik = bod, táhni = hlasitěji/tišeji, dvojklik = smazat bod"),
    "tame": dict(attr="tame_env", master="tame_env", neutral=0.0, vmax=1.0, color=QColor("#ff6b8a"),
                 mcolor=QColor("#ff6b8a"), label="Špičky", what="špiček", hint=HINT_TAME,
                 tip="Kde ztlumit špičky – místa výrazně hlasitější než zbytek (přehnaný drop, ostrá rána).\n"
                     "Křivka nahoře = silně, dole = nechat. Tichá místa se nemění."),
    "gate": dict(attr="gate_env", master="gate_env", neutral=0.0, vmax=1.0, color=QColor("#5ee6a8"),
                 mcolor=QColor("#5ee6a8"), label="Šum", what="šumu", hint=HINT_GATE,
                 tip="Kde odfiltrovat šum – tiché věci pod úrovní zvuku (šum, brum, dozvuky mezi ranami)\n"
                     "se ztlumí. Křivka nahoře = silně, dole = nechat. Hlasité části se nemění."),
}


def env_y(v: float, y0: float, y1: float, cv=CURVES["vol"]) -> float:
    return y1 - v / cv["vmax"] * (y1 - y0)


def env_v(y: float, y0: float, y1: float, cv=CURVES["vol"]) -> float:
    vmax, n = cv["vmax"], cv["neutral"]
    v = (y1 - y) / (y1 - y0) * vmax
    v = round(v / vmax * 20) / 20 * vmax if abs(v - n) > 0.02 * vmax else n   # po 5 %, u neutrální přichytí
    return min(vmax, max(0.0, v))


def env_gain_at(pts: list, beats: np.ndarray):
    if not pts:
        return 1.0
    return np.interp(beats, [q[0] for q in pts], [q[1] for q in pts])


def draw_env(p: QPainter, pts: list, ppb: float, off: float, xl: float, xr: float,
             y0: float, y1: float, color: QColor, handles: bool, drag_pt=None, cv=CURVES["vol"]):
    """Křivka (hlasitost / úpravy); x = doba * ppb - off."""
    ny = env_y(cv["neutral"], y0, y1, cv)
    if handles:
        p.setPen(QPen(QColor(255, 255, 255, 50), 1, Qt.PenStyle.DashLine))
        p.drawLine(QLineF(xl, ny, xr, ny))
    if not pts:
        if handles:
            c = QColor(color)
            c.setAlpha(110)
            p.setPen(QPen(c, 1.5))
            p.drawLine(QLineF(xl, ny, xr, ny))
        return
    poly = [QPointF(xl, env_y(pts[0][1], y0, y1, cv))]
    poly += [QPointF(b * ppb - off, env_y(v, y0, y1, cv)) for b, v in pts]
    poly.append(QPointF(xr, env_y(pts[-1][1], y0, y1, cv)))
    p.setPen(QPen(color, 2 if handles else 1.4))
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawPolyline(QPolygonF(poly))
    if handles:
        p.setPen(QPen(QColor("#000"), 1))
        p.setBrush(color)
        for b, v in pts:
            x = b * ppb - off
            if xl - 6 <= x <= xr + 6:
                p.drawEllipse(QPointF(x, env_y(v, y0, y1, cv)), 4, 4)
    if drag_pt is not None:
        b, v = drag_pt
        x, y = b * ppb - off, env_y(v, y0, y1, cv)
        txt = f"{v * 100:.0f} %"
        r = QRectF(x + 8, max(y0, min(y1 - 16, y - 8)), 52, 16)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 200))
        p.drawRoundedRect(r, 3, 3)
        p.setPen(color)
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, txt)


class _Sig(QObject):
    mix = Signal(int, object)


# ====================================================================== pravítko + vlna mixu

class TopBar(QWidget):
    def __init__(self, post: "PostEditor"):
        super().__init__()
        self.post = post
        self.setFixedHeight(RULER_H + MIX_H)
        self.anchor = None

    def _beat(self, x: float) -> float:
        p = self.post
        return max(0.0, (x + p.hscroll()) / p.ppb)

    ENV_Y = (RULER_H + 5, RULER_H + MIX_H - 5)

    def _in_env(self, y: float) -> bool:
        return self.post.vol_mode and y > RULER_H

    def mousePressEvent(self, ev):
        post = self.post
        pos = ev.position()
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        if self._in_env(pos.y()):
            post.env_press(post.arr, post.cv()["master"], self._beat(pos.x()), pos.y(), *self.ENV_Y)
            return
        b = post.snap_beat(self._beat(pos.x()))
        self.anchor = b
        post.sel = None
        post.seek(b)

    def mouseMoveEvent(self, ev):
        post = self.post
        pos = ev.position()
        if post.env_drag is not None:
            post.env_move(self._beat(pos.x()), pos.y())
            return
        if self.anchor is None:
            return
        b = post.snap_beat(self._beat(pos.x()))
        post.sel = (min(b, self.anchor), max(b, self.anchor)) if abs(b - self.anchor) > 1e-6 else None
        post.update_views()

    def mouseReleaseEvent(self, ev):
        if self.post.env_drag is not None:
            self.post.env_release()
            return
        self.anchor = None
        self.post.update_sel_buttons()

    def mouseDoubleClickEvent(self, ev):
        pos = ev.position()
        if self._in_env(pos.y()):
            self.post.env_delete_at(self.post.arr, self.post.cv()["master"], self._beat(pos.x()), pos.y(),
                                    *self.ENV_Y)

    def contextMenuEvent(self, ev):
        post = self.post
        m = QMenu(self)
        if self._in_env(ev.pos().y()):
            post.env_menu_items(m, post.arr, post.cv()["master"], self._beat(ev.pos().x()), ev.pos().y(),
                                *self.ENV_Y, f"Smazat křivku {post.cv()['what']} mixu")
        if not m.isEmpty():
            m.exec(ev.globalPos())

    def wheelEvent(self, ev):
        self.post.wheel(ev, ev.position().x())

    def paintEvent(self, ev):
        post = self.post
        p = QPainter(self)
        w = self.width()
        off = post.hscroll()
        ppb = post.ppb
        p.fillRect(0, 0, w, RULER_H, QColor("#202328"))
        p.fillRect(0, RULER_H, w, MIX_H, QColor("#0e1012"))
        # výběr
        if post.sel:
            a, b = post.sel
            p.fillRect(QRectF(a * ppb - off, 0, (b - a) * ppb, self.height()), QColor(80, 140, 255, 60))
        # takty
        bar_px = ppb * BEATS_PER_BAR
        step = 1
        while step * bar_px < 90:
            step *= 2
        first = int(off / bar_px) // step * step
        p.setPen(QColor("#9aa3ad"))
        bar = first
        while bar * bar_px - off < w:
            x = bar * bar_px - off
            p.setPen(QColor("#4a5058"))
            p.drawLine(QLineF(x, RULER_H - 8, x, RULER_H))
            p.setPen(QColor("#9aa3ad"))
            secs = bar * BEATS_PER_BAR * 60 / post.arr.bpm
            p.drawText(QRectF(x + 3, 0, 40, RULER_H - 4), Qt.AlignmentFlag.AlignVCenter, str(bar + 1))
            p.setPen(QColor("#6b737c"))
            p.drawText(QRectF(x + 30, 0, 60, RULER_H - 4), Qt.AlignmentFlag.AlignVCenter,
                       f"{int(secs // 60)}:{int(secs % 60):02d}")
            bar += step
        # vlna mixu
        pk = post.mix_peaks
        mid = RULER_H + MIX_H / 2
        hh = MIX_H / 2 - 4
        if pk is not None and len(pk):
            xs = np.arange(w)
            idx = ((xs + off) / ppb / MIX_BUCKET_BEATS).astype(np.int64)
            ok = idx < len(pk)
            xs, idx = xs[ok], idx[ok]
            raw = post.mix_raw
            if raw is not None and len(raw) == len(pk):   # co úpravy (špičky, šum) ubraly
                vals = raw[idx] * hh
                p.setPen(QColor("#7a3b3b"))
                p.drawLines([QLineF(x, mid - v, x, mid + v) for x, v in zip(xs.tolist(), vals.tolist())])
            vals = pk[idx] * hh
            p.setPen(QColor("#5ec8ff"))
            p.drawLines([QLineF(x, mid - v, x, mid + v) for x, v in zip(xs.tolist(), vals.tolist())])
        else:
            p.setPen(QColor("#555"))
            p.drawText(QRectF(8, RULER_H, w, MIX_H), Qt.AlignmentFlag.AlignVCenter, "počítám vlnu mixu…")
        y0, y1 = self.ENV_Y
        for kind, cv in sorted(CURVES.items(), key=lambda kv: kv[0] == post.curve):   # aktivní navrch
            pts, active = getattr(post.arr, cv["master"]), kind == post.curve
            if pts or active:
                draw_env(p, pts, ppb, off, 0, w, y0, y1, cv["mcolor"], active,
                         post.env_drag_pt(post.arr, cv["master"]), cv)
        p.setPen(QColor("#888"))
        p.drawText(QRectF(4, RULER_H + 2, 300, 14), Qt.AlignmentFlag.AlignLeft,
                   f"MIX – {post.cv()['label'].lower()} celku" if post.vol_mode
                   else "MIX · tmavě = před úpravami" if post.mix_raw is not None else "MIX")
        x = post.playhead * ppb - off
        p.setPen(QPen(QColor("#ff5050"), 1.5))
        p.drawLine(QLineF(x, 0, x, self.height()))


# ====================================================================== stopy

class Canvas(QWidget):
    def __init__(self, post: "PostEditor"):
        super().__init__()
        self.post = post
        self.drag = None
        self.setMouseTracking(True)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dragMoveEvent(self, ev):
        if ev.mimeData().hasUrls():
            ev.acceptProposedAction()

    def dropEvent(self, ev):
        files = [u.toLocalFile() for u in ev.mimeData().urls() if u.isLocalFile()]
        if files:
            ev.acceptProposedAction()
            self.post.add_songs(files, self.post.snap_beat(ev.position().x() / self.post.ppb))

    def update_size(self):
        post = self.post
        vp = post.cv_area.viewport()
        end = max(post.arr.end_beat(), post.playhead) + 8 * BEATS_PER_BAR
        self.resize(max(vp.width(), int(end * post.ppb)), max(vp.height(), len(post.arr.lanes) * LANE_H))

    @staticmethod
    def env_y(i: int) -> tuple[float, float]:
        return i * LANE_H + 5, (i + 1) * LANE_H - 5

    def _lane_at(self, y: float):
        i = int(y // LANE_H)
        lanes = self.post.arr.lanes
        return (lanes[i], *self.env_y(i)) if 0 <= i < len(lanes) else (None, 0, 0)

    def _hit(self, pos):
        post = self.post
        i = int(pos.y() // LANE_H)
        lanes = post.arr.lanes
        if not 0 <= i < len(lanes):
            return None, None, None
        lane = lanes[i]
        x = pos.x()
        for r in reversed(lane.regions):
            x1, x2 = r.start * post.ppb, r.end * post.ppb
            if x1 - 3 <= x <= x2 + 3:
                if abs(x - x1) <= 6 and x2 - x1 > 14:
                    return lane, r, "l"
                if abs(x - x2) <= 6:
                    return lane, r, "r"
                return lane, r, "m"
        return lane, None, None

    def paintEvent(self, ev):
        post = self.post
        p = QPainter(self)
        clip = ev.rect()
        ppb = post.ppb
        lanes = post.arr.lanes
        p.fillRect(clip, QColor("#121416"))
        i0 = max(0, clip.top() // LANE_H)
        i1 = min(len(lanes), clip.bottom() // LANE_H + 1)
        for i in range(i0, i1):
            p.fillRect(clip.left(), i * LANE_H, clip.width(), LANE_H, QColor("#181a1d" if i % 2 else "#15171a"))
        # mřížka
        beat_step = 1 if ppb >= 10 else (BEATS_PER_BAR if ppb >= 2.5 else BEATS_PER_BAR * 4)
        b0 = int(clip.left() / ppb) // beat_step * beat_step
        b = b0
        while b * ppb <= clip.right():
            p.setPen(QColor("#2e3238" if b % BEATS_PER_BAR == 0 else "#1e2125"))
            x = b * ppb
            p.drawLine(QLineF(x, clip.top(), x, clip.bottom()))
            b += beat_step
        if post.sel:
            a, bb = post.sel
            p.fillRect(QRectF(a * ppb, clip.top(), (bb - a) * ppb, clip.height()), QColor(80, 140, 255, 40))
        any_solo = any(l.solo for l in lanes)
        sel_ids = {id(r) for r in post.selected}
        for i in range(i0, i1):
            lane = lanes[i]
            col = post.color(lane.ns)
            muted = lane.mute or (any_solo and not lane.solo)
            lp = post.loop_peaks.get(lane.id)
            top = i * LANE_H + 3
            hh = (LANE_H - 10) / 2
            mid = i * LANE_H + LANE_H / 2
            for r in lane.regions:
                x1, x2 = r.start * ppb, r.end * ppb
                if x2 < clip.left() or x1 > clip.right():
                    continue
                fill = QColor(col)
                fill.setAlpha(55 if muted else 135)
                sel = id(r) in sel_ids
                p.setPen(QPen(QColor("#ffffff") if sel else col.darker(140), 2 if sel else 1))
                p.setBrush(fill)
                p.drawRoundedRect(QRectF(x1, top, max(2.0, x2 - x1), LANE_H - 6), 3, 3)
                if lp is not None:
                    peaks, lb = lp
                    xa, xb = max(int(x1) + 1, clip.left()), min(int(x2) - 1, clip.right())
                    if xb > xa:
                        xs = np.arange(xa, xb)
                        ph = ((xs / ppb - r.origin) % lb) / lb
                        v = peaks[np.minimum((ph * len(peaks)).astype(np.int64), len(peaks) - 1)]
                        bx = xs / ppb
                        g = r.gain * lane.volume * 1.25 * env_gain_at(lane.env, bx) * env_gain_at(post.arr.master_env, bx)
                        v = v * np.minimum(1.0, g) * hh
                        p.setPen(QColor(255, 255, 255, 70 if muted else 190))
                        p.drawLines([QLineF(x, mid - h, x, mid + h) for x, h in zip(xs.tolist(), v.tolist())])
                bot = top + LANE_H - 6
                p.setPen(QPen(QColor("#ffffff"), 1))
                p.setBrush(QColor(0, 0, 0, 120))
                if r.fade_in > 0:
                    xf = min(x2, x1 + r.fade_in * ppb)
                    p.drawPolygon(QPolygonF([QPointF(x1, top), QPointF(xf, top), QPointF(x1, bot)]))
                if r.fade_out > 0:
                    xf = max(x1, x2 - r.fade_out * ppb)
                    p.drawPolygon(QPolygonF([QPointF(x2, top), QPointF(xf, top), QPointF(x2, bot)]))
            y0, y1 = self.env_y(i)
            for kind, cv in sorted(CURVES.items(), key=lambda kv: kv[0] == post.curve):
                pts, active = getattr(lane, cv["attr"]), kind == post.curve
                if pts or active:
                    draw_env(p, pts, ppb, 0, clip.left(), clip.right() + 1, y0, y1, cv["color"], active,
                             post.env_drag_pt(lane, cv["attr"]), cv)
        x = post.playhead * ppb
        p.setPen(QPen(QColor("#ff5050"), 1.5))
        p.drawLine(QLineF(x, clip.top(), x, clip.bottom()))

    # ------------------------------------------------------------ myš
    def mousePressEvent(self, ev):
        post = self.post
        pos = ev.position()
        if ev.button() != Qt.MouseButton.LeftButton:
            return
        if post.vol_mode:
            lane, y0, y1 = self._lane_at(pos.y())
            if lane is not None:
                post.env_press(lane, post.cv()["attr"], pos.x() / post.ppb, pos.y(), y0, y1)
            return
        lane, r, edge = self._hit(pos)
        if r is not None:
            if ev.modifiers() & Qt.KeyboardModifier.ControlModifier:
                if any(r is s for s in post.selected):
                    post.selected = [s for s in post.selected if s is not r]
                else:
                    post.selected.append(r)
            elif not any(r is s for s in post.selected):
                post.selected = [r]
            items = post.selected if edge == "m" else [r]
            post.begin_edit()
            self.drag = {"edge": edge, "b0": pos.x() / post.ppb, "moved": False,
                         "items": [(s, s.start, s.end, s.phase) for s in items]}
        else:
            post.selected = []
            post.seek(post.snap_beat(pos.x() / post.ppb))
        post.update_views()

    def mouseMoveEvent(self, ev):
        post = self.post
        pos = ev.position()
        if post.env_drag is not None:
            post.env_move(pos.x() / post.ppb, pos.y())
            return
        if post.vol_mode:
            lane, y0, y1 = self._lane_at(pos.y())
            hit = lane is not None and post.env_hit(getattr(lane, post.cv()["attr"]), pos.x() / post.ppb,
                                                    pos.y(), y0, y1) is not None
            self.setCursor(Qt.CursorShape.SizeVerCursor if hit else Qt.CursorShape.CrossCursor)
            return
        if self.drag is None:
            lane, r, edge = self._hit(pos)
            shape = {"l": Qt.CursorShape.SizeHorCursor, "r": Qt.CursorShape.SizeHorCursor,
                     "m": Qt.CursorShape.OpenHandCursor}.get(edge, Qt.CursorShape.ArrowCursor)
            self.setCursor(shape)
            return
        d = self.drag
        b = pos.x() / post.ppb
        minlen = post.snap if post.snap > 0 else 0.25
        if d["edge"] == "m":
            r0, s0, e0, _ = d["items"][0]
            delta = post.snap_beat(s0 + b - d["b0"]) - s0
            delta = max(delta, -min(s for _, s, _, _ in d["items"]))
            for r, s, e, _ in d["items"]:
                r.start, r.end = s + delta, e + delta
        elif d["edge"] == "l":
            r, s, e, ph = d["items"][0]
            ns = min(max(0.0, post.snap_beat(b)), e - minlen)
            r.start, r.phase = ns, ph - (ns - s)
        else:
            r, s, e, ph = d["items"][0]
            r.end = max(post.snap_beat(b), s + minlen)
        d["moved"] = True
        post.update_views()

    def mouseReleaseEvent(self, ev):
        if self.post.env_drag is not None:
            self.post.env_release()
            return
        if self.drag is None:
            return
        moved = self.drag["moved"]
        self.drag = None
        if moved:
            self.post.commit_edit()
        else:
            self.post.cancel_edit()

    def mouseDoubleClickEvent(self, ev):
        post = self.post
        pos = ev.position()
        if post.vol_mode:
            lane, y0, y1 = self._lane_at(pos.y())
            if lane is not None:
                post.env_delete_at(lane, post.cv()["attr"], pos.x() / post.ppb, pos.y(), y0, y1)
            return
        lane, r, _ = self._hit(pos)
        if lane is None:
            return
        self.drag = None
        post.begin_edit()
        if r is not None:
            lane.regions.remove(r)
            post.selected = []
        else:
            post.add_region(lane, pos.x() / post.ppb)
        post.commit_edit()

    def contextMenuEvent(self, ev):
        post = self.post
        lane, r, _ = self._hit(ev.pos())
        if lane is None:
            return
        b = post.snap_beat(ev.pos().x() / post.ppb)
        m = QMenu(self)
        if post.vol_mode:
            _, y0, y1 = self._lane_at(ev.pos().y())
            post.env_menu_items(m, lane, post.cv()["attr"], ev.pos().x() / post.ppb, ev.pos().y(), y0, y1,
                                f"Smazat křivku {post.cv()['what']} stopy")
            if not m.isEmpty():
                m.exec(ev.globalPos())
            return
        if r is not None:
            m.addAction("Smazat úsek", lambda: post.edit(lambda: lane.regions.remove(r)))
            m.addAction(f"Rozdělit tady (takt {bars_beats(b)})", lambda: post.edit(lambda: Arrangement.split(lane, r, b)))
            m.addAction("Hlasitost úseku…", lambda: post.region_gain(r))
            m.addAction("Duplikovat za", lambda: post.edit(lambda: post.duplicate_after(lane, r)))
            m.addAction("Smyčka od začátku úseku", lambda: post.edit(lambda: setattr(r, "phase", 0.0)))
            for attr, label in (("fade_in", "Nájezd (fade in)"), ("fade_out", "Doznění (fade out)")):
                sub = m.addMenu(label)
                for name, beats in FADES:
                    a = sub.addAction(name, lambda a_=attr, b_=beats: post.edit(lambda: setattr(r, a_, b_)))
                    a.setCheckable(True)
                    a.setChecked(abs(getattr(r, attr) - beats) < 1e-6)
        else:
            m.addAction("Přidat úsek (1 takt) tady", lambda: post.edit(lambda: post.add_region(lane, b)))
        m.addSeparator()
        m.addAction(f"Vložit prázdný takt na {bars_beats(b)}",
                    lambda: post.edit(lambda: post.arr.insert_time(b, BEATS_PER_BAR)))
        m.exec(ev.globalPos())

    def wheelEvent(self, ev):
        self.post.wheel(ev, ev.position().x() - self.post.hscroll())


class LaneHeader(QFrame):
    def __init__(self, post: "PostEditor", lane: Lane):
        super().__init__()
        self.post, self.lane = post, lane
        self.setFixedHeight(LANE_H)
        self.setStyleSheet("LaneHeader { border-bottom: 1px solid #22252a; }")
        h = QHBoxLayout(self)
        h.setContentsMargins(0, 2, 4, 2)
        h.setSpacing(4)
        strip = QFrame()
        strip.setFixedWidth(5)
        strip.setStyleSheet(f"background: {post.color(lane.ns).name()};")
        h.addWidget(strip)
        v = QVBoxLayout()
        v.setSpacing(0)
        title = QLabel()
        title.setText(title.fontMetrics().elidedText(lane.title, Qt.TextElideMode.ElideRight, 130))
        title.setToolTip(f"{lane.title}\n{lane.username} · {lane.license}")
        title.setStyleSheet("font-weight: bold;")
        sub = QLabel("♪ vlastní zvuk" if lane.song else lane.ns)
        sub.setStyleSheet("color: #8a929b; font-size: 10px;")
        v.addWidget(title)
        v.addWidget(sub)
        h.addLayout(v, 1)
        self.m = mkbtn("M", True, "mute", "Ztlumit stopu", width=24)
        self.s = mkbtn("S", True, "solo", "Sólo stopy", width=24)
        set_checked(self.m, lane.mute)
        set_checked(self.s, lane.solo)
        self.m.toggled.connect(lambda on: self._set("mute", on))
        self.s.toggled.connect(lambda on: self._set("solo", on))
        self.vol = mkslider(int(lane.volume * 100), maximum=200, width=56, tip="Hlasitost stopy")
        self.vol.valueChanged.connect(lambda val: self._set("volume", val / 100))
        h.addWidget(self.m)
        h.addWidget(self.s)
        h.addWidget(self.vol)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)

    def _set(self, attr, value):
        setattr(self.lane, attr, value)
        self.post.lanes_changed()

    def _menu(self, pos):
        lane, post = self.lane, self.post
        m = QMenu(self)
        ln = m.addMenu("Délka smyčky")
        for opt in LENGTH_OPTIONS:
            a = ln.addAction(length_label(opt), lambda o=opt: post.edit(lambda: setattr(lane, "length", o)))
            a.setCheckable(True)
            a.setChecked(opt == lane.length)
        a = m.addAction("Napasovat do tempa (⇔)", lambda: post.edit(lambda: setattr(lane, "fit", not lane.fit)))
        a.setCheckable(True)
        a.setChecked(lane.fit)
        if lane.url:
            m.addAction("Otevřít na freesound.org", lambda: QDesktopServices.openUrl(QUrl(lane.url)))
        for cv in CURVES.values():
            if getattr(lane, cv["attr"]):
                m.addAction(f"Smazat křivku {cv['what']}",
                            lambda a=cv["attr"]: post.edit(lambda: setattr(lane, a, [])))
        m.addSeparator()
        m.addAction("Smazat stopu", lambda: post.edit(lambda: post.arr.lanes.remove(lane), rebuild=True))
        m.exec(self.mapToGlobal(pos))


class ExportArrDialog(QDialog):
    def __init__(self, parent, name: str):
        super().__init__(parent)
        self.setWindowTitle("Export postprodukce")
        f = QFormLayout(self)
        self.mix = QCheckBox("Mix (stereo WAV 24 bit)")
        self.mix.setChecked(True)
        self.stems = QCheckBox("Stopy po kategoriích (stems)")
        self.stems.setChecked(True)
        self.lanes = QCheckBox("Každý zvuk zvlášť")
        for w in (self.mix, self.stems, self.lanes):
            f.addRow(w)
        self.folder = QLineEdit(str(config.EXPORT_DIR / f"{name}_export_{datetime.now():%Y%m%d_%H%M%S}"))
        f.addRow("Složka:", self.folder)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)


# ====================================================================== okno

class PostEditor(QWidget):
    def __init__(self, main: "MainWindow", arr: Arrangement, path: Path):
        super().__init__()
        self.main, self.engine, self.svc = main, main.engine, main.svc
        self.arr, self.path = arr, Path(path)
        self.undo_stack: list[dict] = []
        self.redo_stack: list[dict] = []
        self._snap = None
        self.dirty = False
        self.ppb = 14.0
        self.snap = 1.0
        self.playhead = 0.0
        self.sel: tuple[float, float] | None = None
        self.selected: list[Region] = []
        self.curve: str | None = None     # kreslená křivka: vol / tame / gate (None = úpravy úseků)
        self.env_drag: dict | None = None
        self.player = ArrPlayer()
        self.compiled: Compiled | None = None
        self.loop_peaks: dict[str, tuple[np.ndarray, float]] = {}
        self._state_cache: dict = {}
        self._songs: dict[str, np.ndarray] = {}
        self._lp_key: dict = {}
        self.mix_peaks = None
        self.mix_raw = None        # vlna mixu před úpravami (špičky, šum)
        self._lane_fx: dict = {}   # poslední vypočtené úpravy – platí, než se spočítají nové
        self._master_fx = None
        self._mix_gen = 0
        self.mix_pool = ThreadPoolExecutor(max_workers=1)
        self.sig = _Sig()
        self.sig.mix.connect(self._mix_done)
        self.mix_timer = QTimer(self)
        self.mix_timer.setSingleShot(True)
        self.mix_timer.timeout.connect(self._start_mix)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        tb = QHBoxLayout()
        self.play = mkbtn("▶  Přehrát", True, "play", "Přehrát od kurzoru (mezerník)")
        self.play.setMinimumWidth(110)
        self.play.toggled.connect(self.set_play)
        self.pos = QLabel()
        self.pos.setMinimumWidth(150)
        self.pos.setStyleSheet("font-family: monospace; font-size: 14px;")
        self.bpm = nofocus(QDoubleSpinBox())
        self.bpm.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.bpm.setRange(40, 250)
        self.bpm.setDecimals(1)
        self.bpm.setSuffix(" BPM")
        self.bpm.setValue(arr.bpm)
        self.bpm.setToolTip("Tempo nahrávky – jde změnit i zpětně")
        self.bpm.valueChanged.connect(self._bpm_changed)
        self.snapc = nofocus(QComboBox())
        for label, v in SNAPS:
            self.snapc.addItem(label, v)
        self.snapc.setCurrentIndex(1)
        self.snapc.setToolTip("Přichytávání úseků při posunu")
        self.snapc.currentIndexChanged.connect(lambda i: setattr(self, "snap", self.snapc.itemData(i)))
        zo = mkbtn("−", tip="Oddálit (Ctrl+kolečko)", width=28)
        zi = mkbtn("+", tip="Přiblížit (Ctrl+kolečko)", width=28)
        zo.clicked.connect(lambda: self.zoom(0.8))
        zi.clicked.connect(lambda: self.zoom(1.25))
        add = mkbtn("+ Vlastní zvuk",
                    tip="Přidat vlastní soubor (WAV, MP3, FLAC, OGG…) jako novou stopu na pozici kurzoru.\n"
                        "Soubor jde sem i přetáhnout myší.")
        add.clicked.connect(self.pick_songs)
        ins = mkbtn("Vložit takt", tip="Vloží 1 prázdný takt na kurzor – všechno za ním se posune (pauza před drop)")
        ins.clicked.connect(lambda: self.edit(lambda: self.arr.insert_time(self.playhead, BEATS_PER_BAR)))
        self.cut = mkbtn("Vyjmout výběr", tip="Smaže vybraný čas, zbytek se posune doleva")
        self.cut.clicked.connect(lambda: self.sel and self.edit(lambda: self.arr.delete_time(*self.sel), clear_sel=True))
        self.sil = mkbtn("Ztišit výběr", tip="Vypne všechny zvuky ve vybraném čase (čas zůstane)")
        self.sil.clicked.connect(lambda: self.sel and self.edit(lambda: self.arr.clear_range(*self.sel)))
        self.curve_btns = {}
        for kind, cv in CURVES.items():
            b = mkbtn(f"〰 {cv['label']}", True, "vol", cv["tip"])
            b.setStyleSheet(f"QPushButton:checked {{ background: {cv['color'].name()}; color: black; "
                            f"font-weight: bold; }}")
            b.toggled.connect(lambda on, k=kind: self.set_curve(k, on))
            b.setMinimumWidth(b.sizeHint().width() + 16)   # tučný text při zapnutí
            self.curve_btns[kind] = b
        self.undo_b = mkbtn("↶ Zpět", tip="Ctrl+Z")
        self.undo_b.clicked.connect(self.undo)
        self.redo_b = mkbtn("↷ Znovu", tip="Ctrl+Shift+Z")
        self.redo_b.clicked.connect(self.redo)
        save = mkbtn("Uložit", tip="Ctrl+S")
        save.clicked.connect(self.save)
        exp = mkbtn("Export…", tip="Uložit výsledek do WAV")
        exp.clicked.connect(self.export)
        # dva řádky, ať jde okno zmenšit i na menší obrazovku
        for w in (self.play, self.pos, self.bpm, QLabel("Přichytit:"), self.snapc, zo, zi):
            tb.addWidget(w)
        tb.addStretch(1)
        for w in (self.undo_b, self.redo_b):
            tb.addWidget(w)
        root.addLayout(tb)
        tb2 = QHBoxLayout()
        tb2.addWidget(QLabel("Křivka:"))
        for w in (*self.curve_btns.values(), add, ins, self.cut, self.sil):
            tb2.addWidget(w)
        tb2.addStretch(1)
        self.state_lbl = QLabel()
        self.state_lbl.setStyleSheet("color: #8a929b;")
        tb2.addWidget(self.state_lbl)
        for w in (save, exp):
            tb2.addWidget(w)
        root.addLayout(tb2)
        self.hint = QLabel(HINT)
        self.hint.setWordWrap(True)
        self.hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.hint.setStyleSheet("color: #7b838c; font-size: 11px;")
        root.addWidget(self.hint)

        grid = QGridLayout()
        grid.setSpacing(0)
        corner = QLabel("  Stopy")
        corner.setFixedSize(HEAD_W, RULER_H + MIX_H)
        corner.setAlignment(Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignLeft)
        corner.setStyleSheet("background: #202328; color: #9aa3ad; padding: 4px;")
        self.top = TopBar(self)
        self.head_area = QScrollArea()
        self.head_area.setFixedWidth(HEAD_W)
        self.head_area.setWidgetResizable(True)
        self.head_area.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.head_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.head_area.setFrameShape(QFrame.Shape.NoFrame)
        self.canvas = Canvas(self)
        self.cv_area = QScrollArea()
        self.cv_area.setWidget(self.canvas)
        self.cv_area.setFrameShape(QFrame.Shape.NoFrame)
        vb1, vb2 = self.cv_area.verticalScrollBar(), self.head_area.verticalScrollBar()
        vb1.valueChanged.connect(vb2.setValue)
        vb2.valueChanged.connect(vb1.setValue)
        self.cv_area.horizontalScrollBar().valueChanged.connect(lambda _: self.top.update())
        grid.addWidget(corner, 0, 0)
        grid.addWidget(self.top, 0, 1)
        grid.addWidget(self.head_area, 1, 0)
        grid.addWidget(self.cv_area, 1, 1)
        root.addLayout(grid, 1)

        for seq, fn in [(QKeySequence.StandardKey.Undo, self.undo),
                        (QKeySequence.StandardKey.Redo, self.redo),
                        ("Ctrl+Shift+Z", self.redo),
                        (QKeySequence.StandardKey.Save, self.save),
                        (Qt.Key.Key_Delete, self.delete_selected),
                        (Qt.Key.Key_Backspace, self.delete_selected),
                        (Qt.Key.Key_Home, lambda: self.seek(0))]:
            QShortcut(QKeySequence(seq), self, fn)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(30)

        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for lane in self.arr.lanes:
                if not lane.song:
                    self.svc.load_sync(lane.sound())
        finally:
            QApplication.restoreOverrideCursor()
        self.rebuild_heads()
        self.recompile()
        self.update_title()
        self.update_sel_buttons()

    # ------------------------------------------------------------ pomocné
    @property
    def fpb(self) -> float:
        return self.engine.sr * 60.0 / self.arr.bpm

    def hscroll(self) -> int:
        return self.cv_area.horizontalScrollBar().value()

    def color(self, ns: str) -> QColor:
        """Barva kategorie – podle pořadí, zlatý úhel => sousední barvy se vždy liší."""
        order = list(dict.fromkeys(l.ns for l in self.arr.lanes))
        i = order.index(ns) if ns in order else len(order)
        return QColor.fromHsv(int(150 + i * 137.508) % 360, 150, 205)

    def snap_beat(self, b: float) -> float:
        if self.snap <= 0:
            return max(0.0, b)
        return max(0.0, round(b / self.snap) * self.snap)

    def update_title(self):
        self.state_lbl.setText("neuloženo" if self.dirty else "uloženo")

    def update_views(self):
        self.canvas.update()
        self.top.update()

    def update_sel_buttons(self):
        self.cut.setEnabled(self.sel is not None)
        self.sil.setEnabled(self.sel is not None)
        self.undo_b.setEnabled(bool(self.undo_stack))
        self.redo_b.setEnabled(bool(self.redo_stack))

    def rebuild_heads(self):
        w = QWidget()
        v = QVBoxLayout(w)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        for lane in self.arr.lanes:
            v.addWidget(LaneHeader(self, lane))
        v.addStretch(1)
        v.addSpacing(20)
        self.head_area.setWidget(w)
        w.setMinimumHeight(len(self.arr.lanes) * LANE_H + 20)

    # ------------------------------------------------------------ výpočet
    def _state(self, lane: Lane):
        k = (lane.key, str(lane.length), lane.fit, self.arr.bpm)
        st = self._state_cache.get(k)
        if st is None:
            if lane.song:
                buf = self._song_buf(lane.key, lane.path)
            else:
                buf = self.engine.buffers.get(lane.key)
                if buf is None:
                    buf = self.svc.load_sync(lane.sound())
            if buf is None:
                return None, k
            st = prepare_state(buf, self.engine.sr, self.arr.bpm, lane.length, lane.fit, bpm_from_title(lane.title))
            self._state_cache[k] = st
        return st, k

    def recompile(self, mix=True):
        fpb = self.fpb
        any_solo = any(l.solo for l in self.arr.lanes)
        lanes = []
        for lane in self.arr.lanes:
            st, k = self._state(lane)
            if st is None:
                continue
            if self._lp_key.get(lane.id) != k:
                play, L = st
                full = play if len(play) >= L else np.pad(play, ((0, L - len(play)), (0, 0)))
                n = int(max(512, min(L // 256, 400_000)))
                self.loop_peaks[lane.id] = (compute_peaks(full[:L], n), L / fpb)
                self._lp_key[lane.id] = k
            if lane.mute or (any_solo and not lane.solo):
                continue
            regs = region_frames(lane, fpb)
            lanes.append(CompiledLane(lane.id, lane.ns, st, pan_gains(lane.volume, lane.pan), regs,
                                      env_frames(lane.env, fpb), fx_pts(lane), self._lane_fx.get(lane.id)))
        end = max((rg[1] for cl in lanes for rg in cl.regions), default=0)
        # úpravy (špičky, šum) zůstanou z minula, než se v pozadí spočítají nové
        self.compiled = Compiled(self.engine.sr, self.arr.bpm, lanes, end, env_frames(self.arr.master_env, fpb),
                                 fx_pts(self.arr), self._master_fx)
        with self.engine.lock:
            self.player.compiled = self.compiled
        self.canvas.update_size()
        self.update_views()
        if mix:
            self.mix_timer.start(250)

    def _start_mix(self):
        self._mix_gen += 1
        gen, c = self._mix_gen, self.compiled
        bucket = max(1, int(self.fpb * MIX_BUCKET_BEATS))
        master = self.engine.master
        self.mix_pool.submit(lambda: self.sig.mix.emit(gen, analyze_mix(c, bucket, master)))

    def _mix_done(self, gen, res):
        raw, pk, lane_fx, master_fx = res
        if gen != self._mix_gen:
            return
        self._lane_fx, self._master_fx = lane_fx, master_fx
        c = self.compiled
        with self.engine.lock:
            for cl in c.lanes:
                cl.fx = lane_fx.get(cl.lane_id)
            c.fx = master_fx
        self.mix_peaks = pk
        self.mix_raw = raw if (lane_fx or master_fx is not None) else None
        self.top.update()

    def lanes_changed(self):
        self.dirty = True
        self.update_title()
        self.recompile()

    # ------------------------------------------------------------ úpravy + undo
    def begin_edit(self):
        self._snap = self.arr.to_dict()

    def cancel_edit(self):
        self._snap = None

    def commit_edit(self, rebuild=False):
        if self._snap is not None:
            self.undo_stack.append(self._snap)
            del self.undo_stack[:-200]
            self.redo_stack.clear()
        self._snap = None
        for l in self.arr.lanes:
            l.sort()
        self.dirty = True
        self.update_title()
        if rebuild:
            self.rebuild_heads()
        self.recompile()
        self.update_sel_buttons()

    def edit(self, fn, rebuild=False, clear_sel=False):
        self.begin_edit()
        fn()
        if clear_sel:
            self.sel = None
        self.selected = []
        self.commit_edit(rebuild)

    def _restore(self, d: dict):
        self.arr = Arrangement.from_dict(d)
        self.selected = []
        set_value(self.bpm, self.arr.bpm)
        self.rebuild_heads()
        self.dirty = True
        self.update_title()
        self.recompile()
        self.update_sel_buttons()

    def undo(self):
        if self.undo_stack:
            self.redo_stack.append(self.arr.to_dict())
            self._restore(self.undo_stack.pop())

    def redo(self):
        if self.redo_stack:
            self.undo_stack.append(self.arr.to_dict())
            self._restore(self.redo_stack.pop())

    def delete_selected(self):
        if not self.selected:
            return
        ids = {id(r) for r in self.selected}

        def fn():
            for l in self.arr.lanes:
                l.regions = [r for r in l.regions if id(r) not in ids]
        self.edit(fn)

    def add_region(self, lane: Lane, beat: float):
        unit = self.snap if self.snap > 0 else 1.0
        start = max(0.0, math.floor(beat / unit) * unit)
        gains = sorted(r.gain for r in lane.regions) or [0.8]
        lane.regions.append(Region(start, start + BEATS_PER_BAR, gains[len(gains) // 2], 0.0))

    def duplicate_after(self, lane: Lane, r: Region):
        ln = r.end - r.start
        lane.regions.append(Region(r.end, r.end + ln, r.gain, r.phase))

    def region_gain(self, r: Region):
        v, ok = QInputDialog.getInt(self, "Hlasitost úseku", "Hlasitost (%):", int(round(r.gain * 100)), 0, 300)
        if ok:
            self.edit(lambda: setattr(r, "gain", v / 100))

    # ------------------------------------------------------------ hlasitost
    @property
    def vol_mode(self) -> bool:
        """Kreslí se nějaká křivka (hlasitost / špičky / šum)."""
        return self.curve is not None

    def cv(self) -> dict:
        return CURVES[self.curve or "vol"]

    def set_curve(self, kind: str, on: bool):
        if on:
            self.curve = kind
            for k, b in self.curve_btns.items():
                if k != kind:
                    set_checked(b, False)
        elif self.curve == kind:
            self.curve = None
        on = self.curve is not None
        self.hint.setText(self.cv()["hint"] if on else HINT)
        self.hint.setStyleSheet(f"color: {self.cv()['color'].name() if on else '#7b838c'}; font-size: 11px;")
        self.selected = []
        self.canvas.setCursor(Qt.CursorShape.CrossCursor if on else Qt.CursorShape.ArrowCursor)
        self.update_views()

    def env_hit(self, pts: list, beat: float, y: float, y0: float, y1: float) -> int | None:
        best, bd = None, 8.0
        for i, (pb, pv) in enumerate(pts):
            d = max(abs(pb - beat) * self.ppb, abs(env_y(pv, y0, y1, self.cv()) - y))
            if d <= bd:
                best, bd = i, d
        return best

    def env_press(self, owner, attr: str, beat: float, y: float, y0: float, y1: float):
        pts = getattr(owner, attr)
        i = self.env_hit(pts, beat, y, y0, y1)
        self.begin_edit()
        if i is None:
            pt = [self.snap_beat(beat), env_v(y, y0, y1, self.cv())]
            pts.append(pt)
            pts.sort(key=lambda q: q[0])
        else:
            pt = pts[i]
        self.env_drag = {"owner": owner, "attr": attr, "pt": pt, "y0": y0, "y1": y1, "moved": i is None}
        self.recompile(mix=False)

    def env_move(self, beat: float, y: float):
        d = self.env_drag
        pts, pt = getattr(d["owner"], d["attr"]), d["pt"]
        i = next(k for k, q in enumerate(pts) if q is pt)
        lo = pts[i - 1][0] if i > 0 else 0.0
        hi = pts[i + 1][0] if i + 1 < len(pts) else math.inf
        pt[0] = min(max(self.snap_beat(beat), lo), hi)
        pt[1] = env_v(y, d["y0"], d["y1"], self.cv())
        d["moved"] = True
        self.recompile(mix=False)

    def env_release(self):
        moved = self.env_drag["moved"]
        self.env_drag = None
        if moved:
            self.commit_edit()
        else:
            self.cancel_edit()
            self.update_views()

    def env_drag_pt(self, owner, attr: str):
        d = self.env_drag
        return d["pt"] if d is not None and d["owner"] is owner and d["attr"] == attr else None

    def env_delete_at(self, owner, attr: str, beat: float, y: float, y0: float, y1: float):
        i = self.env_hit(getattr(owner, attr), beat, y, y0, y1)
        if i is not None:
            self.edit(lambda: getattr(owner, attr).pop(i))

    def env_menu_items(self, m: QMenu, owner, attr: str, beat: float, y: float, y0: float, y1: float, clear: str):
        i = self.env_hit(getattr(owner, attr), beat, y, y0, y1)
        if i is not None:
            m.addAction("Smazat bod", lambda: self.edit(lambda: getattr(owner, attr).pop(i)))
        if getattr(owner, attr):
            m.addAction(clear, lambda: self.edit(lambda: setattr(owner, attr, [])))

    def _bpm_changed(self, v):
        self.begin_edit()
        ratio = float(v) / self.arr.bpm
        for lane in self.arr.lanes:
            if lane.song:   # písnička má pevnou délku v sekundách
                for r in lane.regions:
                    r.end = r.start + (r.end - r.start) * ratio
                    r.phase *= ratio
        self.arr.bpm = float(v)
        self.commit_edit()

    # ------------------------------------------------------------ vlastní zvuky / písničky
    def _song_buf(self, key: str, path: Path):
        buf = self._songs.get(key)
        if buf is None:
            try:
                buf = library.load_audio(path, max_seconds=SONG_MAX_SECONDS)
            except Exception as ex:
                self.main.log.appendPlainText(f"Nelze načíst {path}: {ex}")
                return None
            self._songs[key] = buf
        return buf

    def pick_songs(self):
        files, _ = QFileDialog.getOpenFileNames(
            self, "Přidat vlastní zvuk / písničku", str(Path.home()),
            "Audio (*.wav *.mp3 *.flac *.ogg *.opus *.aif *.aiff);;Všechny soubory (*)")
        if files:
            self.add_songs(files, self.playhead)

    def add_songs(self, files: list[str], beat: float):
        """Zkopíruje soubory do knihovny (kategorie 'vlastni') a přidá je jako nové stopy."""
        added, bad = [], []
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            for f in files:
                s = library.import_file(Path(f), SONG_NS)
                buf = self._song_buf(s.key, s.path) if s else None
                if buf is None:
                    bad.append(Path(f).name)
                    continue
                added.append((s, len(buf) / self.fpb))
        finally:
            QApplication.restoreOverrideCursor()
        if bad:
            QMessageBox.warning(self, "Vlastní zvuk", "Tyhle soubory nejde načíst:\n" + "\n".join(bad))
        if not added:
            return
        self.begin_edit()
        for i, (s, beats) in enumerate(added):
            lane = Lane(id=f"song:{s.key}:{len(self.arr.lanes)}", key=s.key, title=s.title, ns=s.namespace,
                        file=s.file, length="free", fit=False, volume=1.0, song=True,
                        regions=[Region(beat, beat + beats, 1.0, 0.0)])
            self.arr.lanes.append(lane)
        self.commit_edit(rebuild=True)
        self.cv_area.verticalScrollBar().setValue(self.cv_area.verticalScrollBar().maximum())
        self.main.rescan()
        self.main.statusBar().showMessage(f"Přidáno: {', '.join(s.title for s, _ in added)}", 6000)

    def wheel(self, ev, x_view: float):
        mods = ev.modifiers()
        dy = ev.angleDelta().y() or ev.angleDelta().x()
        if mods & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.25 if dy > 0 else 0.8, x_view)
            ev.accept()
        elif mods & Qt.KeyboardModifier.ShiftModifier:
            hb = self.cv_area.horizontalScrollBar()
            hb.setValue(hb.value() - dy)
            ev.accept()
        else:
            ev.ignore()

    def zoom(self, factor: float, x_view: float | None = None):
        """Zoom; bod pod myší (x_view = x ve viditelné části) zůstane na místě."""
        hb = self.cv_area.horizontalScrollBar()
        if x_view is None:
            x_view = self.playhead * self.ppb - hb.value()
        beat = (hb.value() + x_view) / self.ppb
        self.ppb = min(200.0, max(0.5, self.ppb * factor))
        self.canvas.update_size()
        hb.setValue(int(beat * self.ppb - x_view))
        self.update_views()

    # ------------------------------------------------------------ přehrávání
    def seek(self, beat: float):
        self.playhead = max(0.0, beat)
        if self.player.playing:
            with self.engine.lock:
                self.player.pos = int(self.playhead * self.fpb)
        self.update_views()

    def set_play(self, on: bool):
        if on:
            self.main.stop_live()
            with self.engine.lock:
                self.player.compiled = self.compiled
                self.player.pos = int(self.playhead * self.fpb)
                self.player.playing = True
                self.engine.arr_player = self.player
        else:
            with self.engine.lock:
                self.player.playing = False
        self.play.setText("■  Stop" if on else "▶  Přehrát")

    def tick(self):
        if self.player.playing:
            self.playhead = self.player.pos / self.fpb
            hb = self.cv_area.horizontalScrollBar()
            x = self.playhead * self.ppb
            vw = self.cv_area.viewport().width()
            if x > hb.value() + vw - 40 or x < hb.value():
                hb.setValue(int(x - 60))
            self.update_views()
        elif self.play.isChecked():
            set_checked(self.play, False)
            self.play.setText("▶  Přehrát")
        secs = self.playhead * 60 / self.arr.bpm
        self.pos.setText(f"takt {bars_beats(self.playhead):>6}  {int(secs // 60)}:{secs % 60:04.1f}")

    # ------------------------------------------------------------ soubor
    def save(self):
        self.arr.save(self.path)
        self.dirty = False
        self.update_title()
        self.main.refresh_recordings()

    def export(self):
        if self.compiled is None or not self.compiled.lanes:
            QMessageBox.information(self, "Export", "Nic k exportu.")
            return
        dlg = ExportArrDialog(self, self.arr.name or self.path.stem.replace(".arr", ""))
        if not dlg.exec():
            return
        out = Path(dlg.folder.text()).expanduser()
        QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
        try:
            files = self.do_export(out, dlg.mix.isChecked(), dlg.stems.isChecked(), dlg.lanes.isChecked())
        except Exception as ex:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "Export", f"Export selhal:\n{ex}")
            return
        QApplication.restoreOverrideCursor()
        if QMessageBox.information(self, "Export hotov", f"Uloženo {len(files)} souborů do\n{out}",
                                   QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Ok) \
                == QMessageBox.StandardButton.Open:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(out)))

    def do_export(self, out: Path, mix=True, stems=True, lanes=False) -> list[str]:
        out.mkdir(parents=True, exist_ok=True)
        master = self.engine.master
        c = with_fx(self.compiled, master)   # úpravy (špičky, šum) přesně jako při přehrávání
        files = []
        if mix:
            render_to_file(c, out / "mix.wav", master=master)
            files.append("mix.wav")
        if stems:
            for ns in sorted({cl.ns for cl in c.lanes}):
                ids = {cl.lane_id for cl in c.lanes if cl.ns == ns}
                name = f"stem_{library.slug(ns, 50)}.wav"
                render_to_file(c, out / name, master=master, limiter=False, lane_ids=ids)
                files.append(name)
        if lanes:
            by_id = {l.id: l for l in self.arr.lanes}
            for i, cl in enumerate(c.lanes, 1):
                l = by_id[cl.lane_id]
                name = f"zvuky/{i:02d}_{library.slug(l.title, 50)}.wav"
                render_to_file(c, out / name, master=master, limiter=False, lane_ids={cl.lane_id})
                files.append(name)
        write_credits(out / "CREDITS.txt", self.arr.credits_sounds())
        self.arr.save(out / "postprodukce.arr.json")
        return files

    def shutdown(self):
        """Zastaví přehrávání a uloží změny (při přepnutí nahrávky / zavření aplikace)."""
        if self.play.isChecked():
            self.play.setChecked(False)
        if self.dirty:
            self.save()
        with self.engine.lock:
            if self.engine.arr_player is self.player:
                self.engine.arr_player = None
        self.timer.stop()
        self.mix_timer.stop()
        self.mix_pool.shutdown(wait=False, cancel_futures=True)


class PostPanel(QWidget):
    """Záložka Postprodukce: výběr nahrávky + editor časové osy."""

    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.editor: PostEditor | None = None
        v = QVBoxLayout(self)
        v.setContentsMargins(8, 8, 8, 8)
        top = QHBoxLayout()
        top.addWidget(QLabel("Nahrávka:"))
        self.combo = nofocus(QComboBox())
        self.combo.setMinimumWidth(200)
        self.combo.activated.connect(lambda i: self.open(Path(self.combo.itemData(i))))
        top.addWidget(self.combo)
        dup = mkbtn("Duplikovat", tip="Uložit kopii nahrávky – pro zkoušení různých verzí")
        dup.clicked.connect(self.duplicate)
        rm = mkbtn("Smazat", tip="Smazat nahrávku z disku")
        rm.clicked.connect(self.delete)
        folder = mkbtn("Složka")
        folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(config.EXPORT_DIR))))
        new = mkbtn("+ Nová", tip="Prázdná postprodukce – složíš ji třeba jen z vlastních písniček")
        new.clicked.connect(self.new_empty)
        for w in (new, dup, rm, folder):
            top.addWidget(w)
        top.addStretch(1)
        v.addLayout(top)
        self.placeholder = QLabel("Zatím tu nic není.\n\nNa záložce Mix zapni ● REC, zahraj něco a nahrávání zastav –\n"
                                  "nahrávka se tu objeví jako časová osa, kde jde zpětně upravit,\n"
                                  "kdy který zvuk hrál.\n\nNebo klikni na „+ Nová“ a přidej vlastní písničky.")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet("color: #888; font-size: 16px;")
        v.addWidget(self.placeholder, 1)
        self.body = v

    @staticmethod
    def recordings() -> list[Path]:
        if not config.EXPORT_DIR.exists():
            return []
        return sorted(config.EXPORT_DIR.glob("*.arr.json"), key=lambda p: p.stat().st_mtime, reverse=True)

    def refresh(self):
        cur = self.editor.path if self.editor else None
        self.combo.clear()
        for p in self.recordings():
            try:
                d = json.loads(p.read_text("utf-8"))
                beats = max((r["end"] for l in d.get("lanes", []) for r in l.get("regions", [])), default=0)
                secs = beats * 60 / float(d.get("bpm", 130))
                info = f"{len(d.get('lanes', []))} stop · {int(secs // 60)}:{int(secs % 60):02d}"
            except Exception:
                info = "?"
            self.combo.addItem(f"{p.name.replace('.arr.json', '')}   ({info})", str(p))
        if cur is not None:
            self.combo.setCurrentIndex(max(0, self.combo.findData(str(cur))))

    def open(self, path: Path | None = None):
        if path is None:
            recs = self.recordings()
            if not recs:
                return
            path = recs[0]
        path = Path(path)
        if self.editor is not None and self.editor.path == path:
            return
        try:
            arr = Arrangement.load(path)
        except Exception as ex:
            QMessageBox.critical(self, "Postprodukce", f"Nelze otevřít {path}:\n{ex}")
            return
        self.close_editor()
        self.placeholder.hide()
        self.editor = PostEditor(self.main, arr, path)
        self.body.addWidget(self.editor, 1)
        self.refresh()

    def close_editor(self):
        if self.editor is not None:
            self.editor.shutdown()
            self.editor.deleteLater()
            self.editor = None
        self.placeholder.show()

    def new_empty(self):
        config.EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("mix_%Y%m%d_%H%M%S")
        path = config.EXPORT_DIR / f"{name}.arr.json"
        Arrangement(bpm=self.main.engine.bpm, name=name).save(path)
        self.open(path)

    def duplicate(self):
        if self.editor is None:
            return
        self.editor.save()
        p = self.editor.path
        base = p.name.replace(".arr.json", "")
        i = 2
        while (p.parent / f"{base}_v{i}.arr.json").exists():
            i += 1
        arr = Arrangement.load(p)
        arr.name = f"{base}_v{i}"
        np_ = p.parent / f"{base}_v{i}.arr.json"
        arr.save(np_)
        self.open(np_)

    def delete(self):
        if self.editor is None:
            return
        p = self.editor.path
        if QMessageBox.question(self, "Smazat nahrávku", f"Smazat „{p.name.replace('.arr.json', '')}“ z disku?") \
                != QMessageBox.StandardButton.Yes:
            return
        self.editor.dirty = False
        self.close_editor()
        base = p.name.replace(".arr.json", "")
        for f in (p, p.parent / f"{base}.wav", p.parent / f"{base}_credits.txt"):
            try:
                f.unlink()
            except OSError:
                pass
        self.refresh()
        self.open()

    def toggle_play(self):
        if self.editor is not None:
            self.editor.play.toggle()

    def stop(self):
        if self.editor is not None and self.editor.play.isChecked():
            self.editor.play.setChecked(False)

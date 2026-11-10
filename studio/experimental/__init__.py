"""Experimentální rozšíření „Vzorce“ – hudba podle matematických vzorců.

Zapíná se v menu Nastavení → Experimentální vzorce (uloží se do settings.json),
nebo proměnnou prostředí NSS_EXPERIMENTAL=1 / 0 (má přednost).
Celá složka `experimental` jde z buildu smazat – aplikace pak funguje beze změny
a položka v menu zmizí.
"""
from __future__ import annotations

import json
import os

from .. import config

SETTINGS_FILE = "settings.json"
TAB_TITLE = "Vzorce"


def _settings() -> dict:
    try:
        return json.loads((config.HOME / SETTINGS_FILE).read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def enabled() -> bool:
    env = os.environ.get("NSS_EXPERIMENTAL", "").strip().lower()
    if env in ("1", "true", "ano", "yes"):
        return True
    if env in ("0", "false", "ne", "no"):
        return False
    return bool(_settings().get("experimental"))


def set_enabled(on: bool):
    s = _settings()
    s["experimental"] = bool(on)
    try:
        (config.HOME / SETTINGS_FILE).write_text(json.dumps(s, indent=1), "utf-8")
    except OSError:
        pass


class Extension:
    """Přidá záložku Vzorce do hlavního okna; remove() ji zase odebere."""

    def __init__(self, main):
        from .panel import FormulaPanel
        self.main = main
        self.panel = FormulaPanel(main)
        main.tabs.addTab(self.panel, TAB_TITLE)

    def remove(self):
        self.panel.shutdown()
        if self.panel.a_on.isChecked():
            self.panel.a_on.setChecked(False)
        i = self.main.tabs.indexOf(self.panel)
        if i >= 0:
            self.main.tabs.removeTab(i)
        self.panel.deleteLater()


def install(main) -> Extension:
    return Extension(main)

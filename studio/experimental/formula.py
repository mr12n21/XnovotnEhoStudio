"""Vzorce – bezpečné vyhodnocení matematických výrazů nad numpy poli.

Povolená je jen matematika: čísla, proměnné, + - * / // % **, porovnání,
and/or/not, `a if podmínka else b`, bitové operace (& | ^ ~ << >>) a funkce
z FUNCS. Žádné atributy, importy, indexování ani jiný Python.

Výraz se vyhodnocuje naráz pro celé pole (numpy), proto se and/or/not
a `if else` přepíšou na logical_and / logical_or / logical_not / where.
Celá čísla jsou int64 (žádná nekonečná čísla => vzorec nezamrzne).
"""
from __future__ import annotations

import ast

import numpy as np


class FormulaError(ValueError):
    pass


def _tri(x):
    x = np.asarray(x, np.float64) % 1.0
    return 4 * np.abs(x - 0.5) - 1


def _saw(x):
    return 2 * (np.asarray(x, np.float64) % 1.0) - 1


def _square(x, duty=0.5):
    return np.where(np.asarray(x, np.float64) % 1.0 < duty, 1.0, -1.0)


def _pow(a, b):
    with np.errstate(all="ignore"):
        return np.power(np.asarray(a, np.float64), b)


FUNCS = {
    "sin": np.sin, "cos": np.cos, "tan": np.tan, "asin": np.arcsin, "acos": np.arccos, "atan": np.arctan,
    "exp": np.exp, "log": np.log, "log2": np.log2, "sqrt": np.sqrt, "abs": np.abs, "sign": np.sign,
    "floor": np.floor, "ceil": np.ceil, "round": np.round, "min": np.minimum, "max": np.maximum,
    "clip": np.clip, "where": np.where, "int": lambda x: np.asarray(x).astype(np.int64),
    "float": lambda x: np.asarray(x, np.float64),
    "tri": _tri, "saw": _saw, "square": _square,
}
CONSTS = {"pi": np.pi, "e": np.e, "tau": 2 * np.pi}
# vnitřní funkce, které vkládá přepis výrazu (uživatel je nepíše)
_INTERNAL = {"_and": np.logical_and, "_or": np.logical_or, "_not": np.logical_not, "_where": np.where,
             "_pow": _pow, "_int": np.int64}

_ALLOWED = (ast.Expression, ast.BinOp, ast.UnaryOp, ast.BoolOp, ast.Compare, ast.IfExp, ast.Call, ast.Name,
            ast.Load, ast.Constant, ast.operator, ast.unaryop, ast.boolop, ast.cmpop)


class _Vectorize(ast.NodeTransformer):
    """and/or/not/if-else/řetězená porovnání -> numpy funkce; ** -> _pow; int konstanty -> int64."""

    @staticmethod
    def _call(name, args):
        return ast.Call(func=ast.Name(id=name, ctx=ast.Load()), args=args, keywords=[])

    def visit_BoolOp(self, node):
        self.generic_visit(node)
        fn = "_and" if isinstance(node.op, ast.And) else "_or"
        out = node.values[0]
        for v in node.values[1:]:
            out = self._call(fn, [out, v])
        return out

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Not):
            return self._call("_not", [node.operand])
        return node

    def visit_IfExp(self, node):
        self.generic_visit(node)
        return self._call("_where", [node.test, node.body, node.orelse])

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) == 1:
            return node
        parts, left = [], node.left
        for op, right in zip(node.ops, node.comparators):
            parts.append(ast.Compare(left=left, ops=[op], comparators=[right]))
            left = right
        out = parts[0]
        for p in parts[1:]:
            out = self._call("_and", [out, p])
        return out

    def visit_BinOp(self, node):
        self.generic_visit(node)
        if isinstance(node.op, ast.Pow):
            return self._call("_pow", [node.left, node.right])
        return node

    def visit_Constant(self, node):
        if isinstance(node.value, bool):
            return node
        if isinstance(node.value, int):
            if abs(node.value) >= 2 ** 62:
                raise FormulaError(f"Moc velké číslo: {node.value}")
            return self._call("_int", [node])
        return node


class Formula:
    """Zkompilovaný vzorec. `names` = proměnné, které smí používat (kromě FUNCS a CONSTS)."""

    def __init__(self, text: str, names: set[str], extra_funcs: set[str] = frozenset()):
        self.text = text.strip()
        if not self.text:
            raise FormulaError("Vzorec je prázdný.")
        try:
            tree = ast.parse(self.text, mode="eval")
        except SyntaxError as e:
            raise FormulaError(f"Chyba v zápisu (znak {e.offset or '?'}): {e.msg}") from None
        allowed = set(names) | set(FUNCS) | set(CONSTS) | set(extra_funcs)
        for node in ast.walk(tree):
            if not isinstance(node, _ALLOWED):
                raise FormulaError(f"Tohle ve vzorci nejde: {type(node).__name__}")
            if isinstance(node, ast.Name) and node.id not in allowed:
                raise FormulaError(f"Neznámé jméno: {node.id}")
            if isinstance(node, ast.Call):
                if not isinstance(node.func, ast.Name):
                    raise FormulaError("Volat jde jen funkce jménem, např. sin(x).")
                if node.keywords:
                    raise FormulaError("Funkce berou jen čísla v pořadí, např. clip(x, 0, 1).")
            if isinstance(node, ast.Constant) and not isinstance(node.value, (int, float, bool)):
                raise FormulaError("Ve vzorci můžou být jen čísla.")
        tree = ast.fix_missing_locations(_Vectorize().visit(tree))
        self.code = compile(tree, "<vzorec>", "eval")
        self.names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}

    def uses(self, name: str) -> bool:
        return name in self.names

    def eval(self, ctx: dict, shape: tuple, funcs: dict | None = None) -> np.ndarray:
        """Vyhodnotí vzorec; výsledek má vždy tvar `shape` (float64)."""
        ns = {"__builtins__": {}, **FUNCS, **CONSTS, **_INTERNAL, **(funcs or {}), **ctx}
        try:
            with np.errstate(all="ignore"):
                v = eval(self.code, ns)  # noqa: S307 – strom je ověřený, jen matematika
        except FormulaError:
            raise
        except Exception as e:  # dělení polem nul, špatný počet argumentů…
            raise FormulaError(f"Vzorec nejde spočítat: {e}") from None
        v = np.asarray(v)
        if v.dtype == bool:
            v = v.astype(np.float64)
        try:
            v = np.broadcast_to(v.astype(np.float64), shape)
        except (ValueError, TypeError):
            raise FormulaError(f"Vzorec vrací špatný tvar výsledku {v.shape}.") from None
        return np.nan_to_num(v, nan=0.0, posinf=0.0, neginf=0.0)

"""Checks for "make your own" answers - the student builds something that
fits (two numbers with HCF 6 and LCM 36, an irrational number between 2
and 3), so many answers are right and code decides which.

A question stores {"kind": ..., "params": {...}}; KINDS is the fixed list
a question may use - generated questions pick from it, they never bring
their own code. Each check returns (ok, message): the message says what
the student's answer actually is ("HCF(6, 30) is 6, but the LCM is 30"),
so a wrong answer still teaches.

Student input is untrusted and SymPy's parser evaluates Python, so an
expression is allow-listed first (digits, + - * / ( ) . √ π and the words
sqrt/pi, at most 40 characters, no powers) and parsed with no builtins.
"""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from fractions import Fraction

import sympy
from sympy.parsing.sympy_parser import (
    implicit_multiplication_application,
    parse_expr,
    standard_transformations,
)

MAX_EXPR = 40
MAX_INT = 10**9
_ALLOWED = re.compile(r"^[0-9+\-*/(). ]*$")
_TRANSFORMS = standard_transformations + (implicit_multiplication_application,)


class BadAnswer(ValueError):
    """The answer can't be read - its message is shown to the student."""


@dataclass(frozen=True)
class Check:
    describe: str  # for the generation prompt: what it checks and its params
    fields: int  # how many answer boxes
    run: Callable[[dict, list[str]], tuple[bool, str]]


# ------------------------------------------------------------------ reading answers
def read_int(text: str) -> int:
    text = (text or "").strip().replace(",", "")
    if not re.fullmatch(r"\d{1,10}", text) or int(text) == 0 or int(text) > MAX_INT:
        raise BadAnswer(f'"{text}" isn\'t a whole number we can check.')
    return int(text)


def read_number(text: str) -> sympy.Expr:
    """A real number written like 2 + √3, 3√2, sqrt(5)/2, π or 7/3."""
    raw = (text or "").strip()
    if not raw or len(raw) > MAX_EXPR:
        raise BadAnswer("Write a short number, like √5 or 2 + √3.")
    norm = (raw.replace("×", "*").replace("÷", "/").replace("−", "-").replace("π", " pi ")
            .replace("√", " sqrt ").lower())
    norm = re.sub(r"sqrt\s*(\d+(\.\d+)?)", r"sqrt(\1)", norm)  # √5 -> sqrt(5)
    if "**" in norm or not _ALLOWED.match(re.sub(r"\bsqrt\b|\bpi\b", "", norm)):  # no powers: 9**9**9 hangs
        raise BadAnswer(f'We can\'t read "{raw}". Use numbers, + − × ÷, brackets and √.')
    try:
        expr = parse_expr(norm, local_dict={"sqrt": sympy.sqrt, "pi": sympy.pi},
                          global_dict={"__builtins__": {}, "Integer": sympy.Integer, "Float": sympy.Float,
                                       "Rational": sympy.Rational, "Symbol": sympy.Symbol},
                          transformations=_TRANSFORMS, evaluate=True)
    except Exception as exc:  # noqa: BLE001 - any parse failure is "can't read"
        raise BadAnswer(f'We can\'t read "{raw}". Try something like 2 + √3.') from exc
    if not isinstance(expr, sympy.Expr) or expr.free_symbols or not expr.is_real:
        raise BadAnswer(f'"{raw}" isn\'t a real number.')
    return sympy.nsimplify(expr) if expr.has(sympy.Float) else expr


def _show(expr: sympy.Expr) -> str:
    return str(expr).replace("sqrt", "√").replace("*", "×").replace("pi", "π")


def is_irrational(expr: sympy.Expr) -> bool:
    if expr.is_rational:
        return False
    if expr.is_irrational:
        return True
    raise BadAnswer(f"We couldn't tell whether {_show(expr)} is irrational - try a simpler number.")


# ------------------------------------------------------------------ the checks
def _pair_hcf_lcm(p: dict, answer: list[str]) -> tuple[bool, str]:
    a, b = (read_int(x) for x in answer)
    hcf, lcm = math.gcd(a, b), math.lcm(a, b)
    if (hcf, lcm) == (p["hcf"], p["lcm"]):
        return True, f"HCF({a}, {b}) = {hcf} and LCM = {lcm}. Spot on!"
    parts = [f"HCF({a}, {b}) is {hcf}" + ("" if hcf == p["hcf"] else f", not {p['hcf']}"),
             f"LCM is {lcm}" + ("" if lcm == p["lcm"] else f", not {p['lcm']}")]
    return False, " and the ".join(parts) + "."


def _number_with_primes(p: dict, answer: list[str]) -> tuple[bool, str]:
    n = read_int(answer[0])
    primes = sorted(sympy.factorint(n))
    wanted = sorted(p["primes"])
    shown = " × ".join(f"{q}^{e}" if e > 1 else str(q) for q, e in sympy.factorint(n).items()) or "1"
    if primes != wanted:
        return False, f"{n} = {shown}: its primes are {', '.join(map(str, primes)) or 'none'}, not {', '.join(map(str, wanted))}."
    if n <= p.get("above", 0):
        return False, f"{n} = {shown} - right primes, but it must be more than {p['above']}."
    return True, f"{n} = {shown}. Exactly those primes!"


def _irrational_between(p: dict, answer: list[str]) -> tuple[bool, str]:
    x = read_number(answer[0])
    low, high = Fraction(str(p["low"])), Fraction(str(p["high"]))
    value = float(x.evalf())
    if not is_irrational(x):
        return False, f"{_show(x)} is rational - it can be written as a fraction."
    if not float(low) < value < float(high):
        return False, f"{_show(x)} ≈ {value:.3f}, which isn't between {p['low']} and {p['high']}."
    return True, f"{_show(x)} ≈ {value:.3f}: irrational and between {p['low']} and {p['high']}."


def _irrational_pair(p: dict, answer: list[str]) -> tuple[bool, str]:
    x, y = (read_number(a) for a in answer)
    op = p["result"]  # "sum" | "product"
    for v in (x, y):
        if not is_irrational(v):
            return False, f"{_show(v)} is rational - both numbers must be irrational."
    combined = sympy.simplify(x + y if op == "sum" else x * y)
    word = "sum" if op == "sum" else "product"
    if combined.is_rational:
        return True, f"Both irrational, and their {word} is {_show(combined)} - rational. Nice!"
    return False, f"Their {word} is {_show(combined)}, which is still irrational."


KINDS: dict[str, Check] = {
    "pair_hcf_lcm": Check('two whole numbers with a given HCF and LCM. params {"hcf": int, "lcm": int} '
                          "(lcm a multiple of hcf)", 2, _pair_hcf_lcm),
    "number_with_primes": Check('a whole number whose prime factors are exactly the given primes. params '
                                '{"primes": [int, ...], "above": int (optional lower bound)}', 1, _number_with_primes),
    "irrational_between": Check('an irrational number strictly between two numbers. params {"low": number, '
                                '"high": number}', 1, _irrational_between),
    "irrational_pair": Check('two irrational numbers whose sum or product is rational. params {"result": '
                             '"sum" | "product"}', 2, _irrational_pair),
}


def valid_params(kind: str, params: dict) -> bool:
    """Shape and sense of a question's params (generated questions are checked before they're kept)."""
    try:
        if kind == "pair_hcf_lcm":
            h, l = int(params["hcf"]), int(params["lcm"])
            return 0 < h <= l <= 10000 and l % h == 0
        if kind == "number_with_primes":
            primes = [int(q) for q in params["primes"]]
            return 0 < len(primes) <= 4 and all(sympy.isprime(q) and q < 100 for q in primes)
        if kind == "irrational_between":
            return float(params["low"]) < float(params["high"])
        if kind == "irrational_pair":
            return params["result"] in ("sum", "product")
    except (KeyError, TypeError, ValueError):
        return False
    return False


def check(kind: str, params: dict, answer: list[str]) -> tuple[bool, str]:
    """(ok, message) - an unreadable answer is not ok, with what to fix."""
    spec = KINDS.get(kind)
    if spec is None:
        raise KeyError(kind)
    if len(answer) != spec.fields:
        return False, f"Fill in all {spec.fields} boxes."
    try:
        return spec.run(params, answer)
    except BadAnswer as exc:
        return False, str(exc)

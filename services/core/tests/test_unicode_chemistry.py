"""C-007 (b): scripts/unicode_chemistry.py turns simple chemistry LaTeX into Unicode and
leaves Maths for the app's renderer; converting twice changes nothing."""
import importlib.util
from pathlib import Path

import pytest


def _script():
    path = Path(__file__).resolve().parents[3] / "scripts" / "unicode_chemistry.py"
    if not path.is_file():
        pytest.skip("scripts/ not available in this environment")
    spec = importlib.util.spec_from_file_location("unicode_chemistry", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_simple_chemistry_becomes_unicode_and_maths_stays():
    convert = _script().convert
    cases = {
        r"2NaOH + xAl2O3 {tex}\to{/tex} yNaAlO2 + H2O": "2NaOH + xAl2O3 → yNaAlO2 + H2O",
        r"$2\mathrm{NaOH} + \mathrm{H}_2\mathrm{SO}_4 \rightarrow \mathrm{Na}_2\mathrm{SO}_4$":
            "2NaOH + H₂SO₄ → Na₂SO₄",
        r"Calcium hydroxide + Carbon dioxide \rightarrow Calcium carbonate": "Calcium hydroxide + Carbon dioxide → Calcium carbonate",
        r"The ion $\mathrm{SO}_4^{2-}$ is": "The ion SO₄²⁻ is",
        r"Find $\frac{1}{2}$ of $x_1$.": r"Find $\frac{1}{2}$ of $x_1$.",  # Maths: the renderer's
        r"Show $\mathrm{H}_{2} \xrightarrow{heat}$": r"Show $\mathrm{H}_{2} \xrightarrow{heat}$",  # not simple
    }
    for before, after in cases.items():
        assert convert(before) == after
        assert convert(after) == after

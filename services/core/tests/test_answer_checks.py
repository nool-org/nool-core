"""Make-your-own answer checks: many right answers, a message that says what
the student's answer really is, and untrusted input that can't run code or
hang the server. Pure - no database."""

import time

import pytest

from src.services import answer_checks as checks
from src.services.answer_checks import BadAnswer, check, read_number, valid_params


def test_pair_with_given_hcf_and_lcm_accepts_any_right_pair():
    p = {"hcf": 6, "lcm": 36}
    assert check("pair_hcf_lcm", p, ["12", "18"])[0]
    assert check("pair_hcf_lcm", p, ["36", "6"])[0]
    ok, msg = check("pair_hcf_lcm", p, ["6", "30"])
    assert not ok and msg == "HCF(6, 30) is 6 and the LCM is 30, not 36."
    assert check("pair_hcf_lcm", p, ["12"]) == (False, "Fill in all 2 boxes.")
    assert check("pair_hcf_lcm", p, ["12", "abc"])[1] == '"abc" isn\'t a whole number we can check.'


def test_number_with_exactly_these_primes():
    p = {"primes": [2, 3], "above": 50}
    assert check("number_with_primes", p, ["72"]) == (True, "72 = 2^3 × 3^2. Exactly those primes!")
    assert not check("number_with_primes", p, ["30"])[0]  # has a 5
    assert "more than 50" in check("number_with_primes", p, ["12"])[1]


def test_irrational_between_reads_school_notation():
    p = {"low": 2, "high": 3}
    for answer in ("√5", "sqrt(7)", "2 + √3/4", "(√2 + √8)/2", "π - 0.5", "5√3/3"):
        assert check("irrational_between", p, [answer])[0], answer
    assert check("irrational_between", p, ["5/2"]) == (False, "5/2 is rational - it can be written as a fraction.")
    assert "isn't between 2 and 3" in check("irrational_between", p, ["√2"])[1]
    assert check("irrational_between", p, ["√4 + 0.5"])[0] is False  # √4 = 2: rational


def test_two_irrationals_with_a_rational_sum_or_product():
    assert check("irrational_pair", {"result": "product"}, ["√2", "√8"])[0]
    assert check("irrational_pair", {"result": "sum"}, ["2 + √3", "2 − √3"])[0]
    ok, msg = check("irrational_pair", {"result": "product"}, ["√2", "√3"])
    assert not ok and "still irrational" in msg
    assert "both numbers must be irrational" in check("irrational_pair", {"result": "sum"}, ["2", "√3"])[1]


@pytest.mark.parametrize("hostile", [
    "__import__('os').system('x')", "().__class__", "exec(1)", "lambda: 1", "a+b", "9**9**9**9",
    "1" * 41, "", "[1,2]", "2^10",
])
def test_untrusted_input_is_refused_not_run(hostile):
    started = time.monotonic()
    with pytest.raises(BadAnswer):
        read_number(hostile)
    assert time.monotonic() - started < 1


def test_generated_params_are_validated_before_use():
    assert valid_params("pair_hcf_lcm", {"hcf": 6, "lcm": 36})
    assert not valid_params("pair_hcf_lcm", {"hcf": 5, "lcm": 36})  # lcm not a multiple
    assert valid_params("number_with_primes", {"primes": [2, 5], "above": 10})
    assert not valid_params("number_with_primes", {"primes": [4]})
    assert not valid_params("irrational_between", {"low": 3, "high": 2})
    assert not valid_params("nope", {})
    assert set(checks.KINDS) == {"pair_hcf_lcm", "number_with_primes", "irrational_between", "irrational_pair"}

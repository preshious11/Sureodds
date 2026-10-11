"""Golden comparison with a narrow, explicit float tolerance.

Root cause it addresses: the Poisson/devig chains call math.exp / math.log /
math.lgamma, whose implementations differ by ~1 ulp between Windows (MSVC)
and Linux (glibc) libm. The recorded goldens are therefore bit-exact on one
platform and 1-ulp off on the other, in the last printed digit only.

What stays STRICT (exact): every dict key set, every list length and order,
every int/str/bool/None leaf, and every JSON type. Only float-vs-float (or
float-vs-int) leaves use math.isclose with rel_tol=1e-12, abs_tol=1e-15:
~1,000x above observed libm noise (~1e-16) and ~100,000x below the smallest
measured decision margin (rank gap 9.8e-07; see test_golden_decision_margins),
so the tolerance cannot mask a changed pick, price, ordering, or threshold
decision.
"""
from __future__ import annotations

import json
import math

REL_TOL = 1e-12
ABS_TOL = 1e-15


class GoldenMismatch(AssertionError):
    """Raised with the JSON path of the first difference."""


def _eq(actual, expected, path: str) -> None:
    if isinstance(actual, bool) or isinstance(expected, bool):
        if actual is not expected and actual != expected:
            raise GoldenMismatch(f"{path}: {actual!r} != {expected!r}")
        if type(actual) is not type(expected):
            raise GoldenMismatch(f"{path}: type {type(actual).__name__} != {type(expected).__name__}")
        return
    if isinstance(actual, dict) and isinstance(expected, dict):
        if set(actual) != set(expected):
            missing = sorted(set(expected) - set(actual))
            extra = sorted(set(actual) - set(expected))
            raise GoldenMismatch(f"{path}: keys differ (missing={missing}, extra={extra})")
        for key in actual:
            _eq(actual[key], expected[key], f"{path}.{key}")
        return
    if isinstance(actual, list) and isinstance(expected, list):
        if len(actual) != len(expected):
            raise GoldenMismatch(f"{path}: length {len(actual)} != {len(expected)}")
        for i, (a, e) in enumerate(zip(actual, expected)):
            _eq(a, e, f"{path}[{i}]")
        return
    if isinstance(actual, (int, float)) and isinstance(expected, (int, float)) \
            and not isinstance(actual, bool) and not isinstance(expected, bool):
        if isinstance(actual, float) or isinstance(expected, float):
            if not math.isclose(float(actual), float(expected), rel_tol=REL_TOL, abs_tol=ABS_TOL):
                raise GoldenMismatch(f"{path}: {actual!r} != {expected!r} (beyond 1e-12)")
            return
        if actual != expected:
            raise GoldenMismatch(f"{path}: {actual!r} != {expected!r}")
        return
    if actual != expected or type(actual) is not type(expected):
        raise GoldenMismatch(f"{path}: {actual!r} != {expected!r}")


def assert_golden_equal(actual_text: str, expected_text: str, label: str = "golden") -> None:
    """Compare two golden_text outputs: exact structure, tolerant floats."""
    try:
        _eq(json.loads(actual_text), json.loads(expected_text), label)
    except json.JSONDecodeError as exc:
        raise GoldenMismatch(f"{label}: invalid JSON: {exc}") from exc

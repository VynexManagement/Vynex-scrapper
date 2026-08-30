"""The accuracy gate.

Measures per-signal precision and recall against hand-labelled real storefronts
in `tests/fixtures/*.json`, captured with `tools/capture_fixture.py`.

**Precision is the gate, not recall.** Missing a store costs nothing — there are
plenty. Wrongly claiming a gap costs a wasted email and, because we ship the
evidence alongside every row, it is a *visible* error a buyer can catch in
seconds. BuiltWith survives ~80% accuracy only because its mistakes are
invisible; ours are not, so the bar is higher.

A signal that cannot clear PRECISION_FLOOR on real fixtures should be cut, not
shipped with a caveat.
"""

import glob
import json
import os

import pytest

from signals import generate_signals

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

PRECISION_FLOOR = 0.95
# Below this many labelled examples a precision figure is noise, not evidence.
MIN_SAMPLES = 5


def load_fixtures() -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(FIXTURE_DIR, "*.json"))):
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        data["_path"] = os.path.basename(path)
        out.append(data)
    return out


def evaluate() -> dict[str, dict[str, int]]:
    """Return per-signal {tp, fp, fn, tn} across all labelled fixtures."""
    tally: dict[str, dict[str, int]] = {}

    for fixture in load_fixtures():
        labels = fixture.get("labels") or {}
        if not labels:
            continue
        predicted, _ = generate_signals(
            fixture["fingerprint"],
            fixture.get("fetch_quality") or {"scoreable": True},
        )
        for slug, truth in labels.items():
            counts = tally.setdefault(slug, {"tp": 0, "fp": 0, "fn": 0, "tn": 0})
            fired = slug in predicted
            if fired and truth:
                counts["tp"] += 1
            elif fired and not truth:
                counts["fp"] += 1
            elif not fired and truth:
                counts["fn"] += 1
            else:
                counts["tn"] += 1
    return tally


def _precision(counts: dict[str, int]) -> float:
    denom = counts["tp"] + counts["fp"]
    return counts["tp"] / denom if denom else 1.0


def _recall(counts: dict[str, int]) -> float:
    denom = counts["tp"] + counts["fn"]
    return counts["tp"] / denom if denom else 1.0


def test_fixtures_present():
    fixtures = load_fixtures()
    if not fixtures:
        pytest.skip(
            "No labelled fixtures yet. Capture them with:\n"
            "  python tools/capture_fixture.py <store-url> --label no_email_marketing=true ...\n"
            "Target 40-60 stores before trusting any precision number."
        )
    assert len(fixtures) >= 1


def test_precision_meets_floor():
    tally = evaluate()
    if not tally:
        pytest.skip("No labelled fixtures yet — see test_fixtures_present.")

    failures = []
    report_lines = []
    for slug in sorted(tally):
        counts = tally[slug]
        samples = counts["tp"] + counts["fp"] + counts["fn"] + counts["tn"]
        precision, recall = _precision(counts), _recall(counts)
        report_lines.append(
            f"  {slug:<24} n={samples:<4} precision={precision:.2f} recall={recall:.2f} "
            f"(tp={counts['tp']} fp={counts['fp']} fn={counts['fn']})"
        )
        if samples >= MIN_SAMPLES and precision < PRECISION_FLOOR:
            failures.append(f"{slug}: precision {precision:.2f} < {PRECISION_FLOOR}")

    print("\nPer-signal accuracy:\n" + "\n".join(report_lines))
    assert not failures, "Signals below the precision floor:\n  " + "\n  ".join(failures)


def test_no_signals_from_unscoreable_fixtures():
    """Any fixture captured below the quality floor must produce nothing."""
    for fixture in load_fixtures():
        quality = fixture.get("fetch_quality") or {}
        if quality.get("scoreable") is False:
            predicted, _ = generate_signals(fixture["fingerprint"], quality)
            assert predicted == {}, f"{fixture['_path']} emitted signals despite failing quality gating"

"""Tests des règles par défaut et de leur application."""

from __future__ import annotations

import json
from pathlib import Path

from budget_poc.categorizer import DEFAULT_RULES, UNKNOWN_CATEGORY, Categorizer

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_reference_file_matches_code_defaults() -> None:
    reference = json.loads((_REPO_ROOT / "rules.default.json").read_text(encoding="utf-8"))
    assert reference == DEFAULT_RULES


def test_first_matching_rule_wins_and_sense_is_respected(tmp_path: Path) -> None:
    categorizer = Categorizer(tmp_path / "rules.json")  # créé depuis les défauts
    assert categorizer.categorize("UBER EATS PARIS", -18.0) == "Restauration"
    assert categorizer.categorize("UBER TRIP", -9.0) == "Transport"
    assert categorizer.categorize("VIR SEPA INST RECU DE X", 50.0) == "Virement reçu"
    assert categorizer.categorize("VIR SEPA INST RECU DE X", -50.0) == UNKNOWN_CATEGORY

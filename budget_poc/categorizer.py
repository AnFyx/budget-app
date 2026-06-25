"""Catégorisation des transactions par règles ordonnées.

Principe clé : la catégorie n'est PAS stockée dans la transaction, elle est
dérivée à la lecture en appliquant des règles au libellé. Modifier une règle
re-catégorise donc instantanément tout l'historique, sans toucher la base.

Les règles forment une LISTE ORDONNÉE : la première qui matche gagne. On les
ordonne du plus spécifique au plus général. Chaque règle peut filtrer sur le
sens (débit/crédit), ce qui distingue p. ex. un virement reçu d'un virement émis,
ou un remboursement d'un achat chez le même marchand.

Le fichier rules.json (dans data_dir) est éditable par l'utilisateur et, à terme,
par le front. S'il n'existe pas, il est créé à partir des règles par défaut.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Literal

logger = logging.getLogger(__name__)

UNKNOWN_CATEGORY = "Inconnue"
_VALID_TYPES = {"any", "debit", "credit"}

RuleType = Literal["any", "debit", "credit"]

# Jeu de règles par défaut, volontairement générique (marchands nationaux et
# libellés bancaires courants). Les règles personnelles de l'utilisateur vivent
# dans son propre rules.json (data_dir), jamais dans le dépôt.
# Ordre = priorité (première qui matche gagne) : du plus spécifique au plus
# général. Les `keywords` sont matchés en sous-chaîne sur le libellé normalisé
# (minuscules). `type` filtre sur le sens du montant.
# `rules.default.json` à la racine est une copie de référence de cette liste
# (cohérence vérifiée par les tests).
DEFAULT_RULES: list[dict] = [
    # --- Mécaniques bancaires (avant tout le reste) ---
    {"category": "Frais bancaires", "type": "debit", "keywords": ["cotisation", "frais bancaires"]},
    # Un remboursement carte doit primer sur le marchand concerné.
    {"category": "Remboursement", "type": "credit", "keywords": ["rembourst cb", "remboursement"]},
    {"category": "Retrait espèces", "type": "debit", "keywords": ["retrait dab"]},
    {"category": "Crédit", "type": "debit", "keywords": ["echeance pret"]},
    {"category": "Salaire", "type": "credit", "keywords": ["salaire"]},
    # --- Charges fixes ---
    {"category": "Logement", "type": "debit", "keywords": ["loyer"]},
    {"category": "Énergie", "type": "debit", "keywords": ["edf", "engie", "totalenergies"]},
    {
        "category": "Télécom & internet",
        "type": "debit",
        "keywords": ["free mobile", "free telecom", "orange", "sfr", "bouygues telecom"],
    },
    {
        "category": "Abonnements",
        "type": "debit",
        "keywords": ["netflix", "spotify", "deezer", "disney plus", "amazon prime"],
    },
    # --- Vie courante ---
    {"category": "Santé", "type": "any", "keywords": ["pharmacie"]},
    {
        "category": "Restauration",
        "type": "any",
        "keywords": ["restaurant", "mcdonald", "burger king", "kfc", "uber eats", "deliveroo"],
    },
    {"category": "Boulangerie", "type": "any", "keywords": ["boulangerie"]},
    {
        "category": "Courses",
        "type": "any",
        "keywords": [
            "carrefour", "leclerc", "auchan", "lidl", "intermarche",
            "monoprix", "franprix", "super u",
        ],
    },
    {"category": "Carburant", "type": "debit", "keywords": ["station service", "esso", "shell"]},
    {"category": "Transport", "type": "debit", "keywords": ["sncf", "ratp", "uber", "blablacar"]},
    {
        "category": "Shopping",
        "type": "any",
        "keywords": ["amazon", "fnac", "decathlon", "zalando", "vinted"],
    },
    {"category": "Voyage & hébergement", "type": "any", "keywords": ["airbnb", "booking.com"]},
    # --- Virements : génériques, en dernier ---
    {"category": "Virement interne", "type": "any", "keywords": ["vir cpte a cpte"]},
    {
        "category": "Virement reçu",
        "type": "credit",
        "keywords": ["vir sepa recu", "vir sepa inst recu"],
    },
    {
        "category": "Virement émis",
        "type": "debit",
        "keywords": ["vir sepa emis", "vir sepa inst emis"],
    },
]


class CategorizerError(RuntimeError):
    """Erreur de chargement ou de validation des règles."""


def load_rules(rules_path: Path) -> list[dict]:
    """Charge et valide rules.json, ou l'initialise depuis les défauts.

    Fonction réutilisable par la CLI et le front. Valide chaque règle (entrée
    éditable, donc non fiable).
    """
    if not rules_path.is_file():
        rules_path.parent.mkdir(parents=True, exist_ok=True)
        rules_path.write_text(
            json.dumps(DEFAULT_RULES, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("Règles par défaut écrites dans %s", rules_path)
        return list(DEFAULT_RULES)

    try:
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        raise CategorizerError("rules.json illisible ou JSON invalide.") from exc
    if not isinstance(rules, list):
        raise CategorizerError("rules.json doit contenir une liste de règles.")
    for i, rule in enumerate(rules):
        _validate_rule(rule, i)
    return rules


def save_rules(rules_path: Path, rules: list[dict]) -> None:
    """Valide puis écrit la liste de règles dans rules.json (écriture atomique)."""
    for i, rule in enumerate(rules):
        _validate_rule(rule, i)
    tmp_path = rules_path.with_suffix(".json.tmp")
    tmp_path.write_text(
        json.dumps(rules, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    tmp_path.replace(rules_path)


# Motifs d'extraction du mot-clé suggéré à partir d'un libellé brut.
_CARD_MERCHANT_RE = re.compile(r"FACTURE CARTE DU \d{6} (.+?) CARTE \d", re.IGNORECASE)
_BENEFICIARY_RE = re.compile(r"/BEN ([A-Za-z0-9 ]+?)(?: /|$)")
_SENDER_RE = re.compile(r"/DE ([A-Za-z .]+?) /")


def suggest_keyword(label: str) -> str:
    """Propose un mot-clé de règle à partir d'un libellé brut.

    Heuristiques, par ordre : marchand d'un paiement carte (entre la date et
    'CARTE'), sinon bénéficiaire d'un virement (/BEN ...), sinon émetteur
    (/DE ...), sinon les premiers mots du libellé. Le résultat est destiné à
    être affiché PRÉ-REMPLI puis édité par l'utilisateur, jamais appliqué tel quel.
    """
    card = _CARD_MERCHANT_RE.search(label)
    if card:
        return card.group(1).strip()
    ben = _BENEFICIARY_RE.search(label)
    if ben:
        return ben.group(1).strip()
    sender = _SENDER_RE.search(label)
    if sender:
        return sender.group(1).strip()
    # Repli : les premiers mots, en ignorant un éventuel préfixe technique.
    return " ".join(label.split()[:3])


def resolve_category(
    categorizer: "Categorizer",
    overrides: dict[str, str],
    dedup_key: str,
    label: str,
    amount: float,
) -> str:
    """Catégorie finale d'une transaction : l'override prime sur les règles.

    Ordre de résolution : correction manuelle (override par dedup_key) >
    première règle qui matche > 'Inconnue'. Centralisé ici pour que la CLI et le
    front donnent exactement le même résultat.
    """
    override = overrides.get(dedup_key)
    if override:
        return override
    return categorizer.categorize(label, amount)


def _normalize(label: str) -> str:
    """Normalise un libellé pour le matching : minuscules, espaces compactés."""
    return " ".join(label.lower().split())


def _validate_rule(rule: dict, index: int) -> None:
    """Valide la structure d'une règle (entrée éditable, donc non fiable)."""
    if not isinstance(rule, dict):
        raise CategorizerError(f"Règle #{index} : objet attendu.")
    if not isinstance(rule.get("category"), str) or not rule["category"]:
        raise CategorizerError(f"Règle #{index} : 'category' manquante.")
    if rule.get("type", "any") not in _VALID_TYPES:
        raise CategorizerError(
            f"Règle #{index} : 'type' doit être any/debit/credit."
        )
    keywords = rule.get("keywords")
    if not isinstance(keywords, list) or not all(
        isinstance(k, str) for k in keywords
    ):
        raise CategorizerError(f"Règle #{index} : 'keywords' doit être une liste de str.")


class Categorizer:
    """Applique un jeu de règles ordonnées à des libellés de transaction."""

    def __init__(self, rules_path: Path) -> None:
        self._rules_path = rules_path
        self._rules = load_rules(rules_path)
        self._compiled = self._compile(self._rules)

    @property
    def rules(self) -> list[dict]:
        """Liste des règles actuellement chargées."""
        return self._rules

    def _compile(self, rules: list[dict]) -> list[tuple]:
        """Pré-compile les règles en (category, type, matcher) pour la perf.

        Si une règle porte `regex: true`, ses keywords sont compilés en motifs
        regex (insensibles à la casse). Sinon, simple test de sous-chaîne.
        """
        compiled: list[tuple] = []
        for rule in rules:
            category = rule["category"]
            rule_type = rule.get("type", "any")
            keywords = rule.get("keywords", [])
            if rule.get("regex"):
                try:
                    patterns = [re.compile(k, re.IGNORECASE) for k in keywords]
                except re.error as exc:
                    raise CategorizerError(
                        f"Regex invalide dans la règle '{category}'."
                    ) from exc
                matcher = ("regex", patterns)
            else:
                matcher = ("substring", [k.lower() for k in keywords])
            compiled.append((category, rule_type, matcher))
        return compiled

    def categorize(self, label: str, amount: float) -> str:
        """Retourne la catégorie d'une transaction, ou 'Inconnue' si aucune règle.

        `amount` est signé (négatif = débit). Le sens sert à filtrer les règles
        dont le `type` est restreint à débit ou crédit.
        """
        normalized = _normalize(label)
        sense = "debit" if amount < 0 else "credit"
        for category, rule_type, (kind, patterns) in self._compiled:
            if rule_type != "any" and rule_type != sense:
                continue
            if kind == "substring":
                if any(keyword in normalized for keyword in patterns):
                    return category
            else:  # regex
                if any(pattern.search(normalized) for pattern in patterns):
                    return category
        return UNKNOWN_CATEGORY

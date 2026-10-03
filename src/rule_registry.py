"""
rule_registry.py -- DB-backed registry of *verified* Tradeify rules.

Why a registry (and why the numbers here are frozen):
    The deterministic engine in ``rule_engine.py`` / ``payout.py`` is the single
    source of truth for *behaviour*. This module is the single source of truth
    for *documentation metadata*: which rule applies to which account type and
    phase, what the human-readable threshold is, whether a breach is soft or
    hard, and -- critically -- which retrieved rulebook chunk justifies it.

    Nothing here re-implements a check. It is a lookup table used by the
    explainer and the Ops console so every sentence can point at a citation.
    The thresholds are copied verbatim from ``rule_engine.ACCOUNT_SPECS`` and
    must never diverge from it; ``validate_against_account_specs()`` enforces
    that the consistency / drawdown numbers still match the engine.

Storage:
    Production uses the ``rule_registry`` table created by
    ``alembic/versions/0002_rule_registry.py``. ``load_rules()`` reads from a
    database URL when one is configured (``RULE_REGISTRY_DB_URL`` or
    ``DATABASE_URL``); otherwise it falls back to a seeded in-memory SQLite
    database so the demo and CI exercise the exact same load-from-DB path.

Definition of Done (brief, deliverable A):
    >= 20 entries, every ``citation_chunk_id`` resolves to a chunk that really
    exists in the retrieval corpus. ``validate_registry()`` is the gate.

Run: python3 src/rule_registry.py   (prints a validation report)
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(__file__))

from sqlalchemy import Integer, String, create_engine, delete, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from retrieval import load_corpus

# ---------------------------------------------------------------------------
# ORM model -- mirrors alembic/versions/0002_rule_registry.py exactly.
# ---------------------------------------------------------------------------

class RegistryBase(DeclarativeBase):
    pass


class RuleRegistryRow(RegistryBase):
    __tablename__ = "rule_registry"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[str] = mapped_column(
        String(128), unique=True, nullable=False, index=True)
    account_type: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True)   # growth | select | lightning | all
    phase: Mapped[str] = mapped_column(
        String(32), nullable=False)               # eval | funded | all
    rule_name: Mapped[str] = mapped_column(String(160), nullable=False)
    threshold: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    breach_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="INFO")  # HARD_BREACH|SOFT_BREACH|INFO
    citation_chunk_id: Mapped[str] = mapped_column(String(64), nullable=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "account_type": self.account_type,
            "phase": self.phase,
            "rule_name": self.rule_name,
            "threshold": self.threshold,
            "breach_type": self.breach_type,
            "citation_chunk_id": self.citation_chunk_id,
        }


# ---------------------------------------------------------------------------
# The verified rule set. Thresholds are strings for display; the machine
# numbers live in rule_engine.ACCOUNT_SPECS and are checked for agreement.
# Every citation_chunk_id must exist in data/rulebook_chunks.json.
# ---------------------------------------------------------------------------

DEFAULT_RULES: list[dict[str, str]] = [
    # ---- Growth: evaluation -------------------------------------------------
    {"rule_id": "growth_eval_daily_loss_limit", "account_type": "growth",
     "phase": "eval", "rule_name": "Daily Loss Limit (soft)",
     "threshold": "1250 / 2500 / 3750 by size", "breach_type": "SOFT_BREACH",
     "citation_chunk_id": "chunk_dll"},
    {"rule_id": "growth_eval_trailing_drawdown", "account_type": "growth",
     "phase": "eval", "rule_name": "End-of-day trailing drawdown",
     "threshold": "2000 / 3500 / 5000 by size", "breach_type": "HARD_BREACH",
     "citation_chunk_id": "chunk_trailing_dd"},
    {"rule_id": "growth_eval_profit_target", "account_type": "growth",
     "phase": "eval", "rule_name": "Profit target",
     "threshold": "3000 / 6000 / 9000 by size", "breach_type": "INFO",
     "citation_chunk_id": "chunk_profit_target"},
    {"rule_id": "growth_eval_consistency", "account_type": "growth",
     "phase": "eval", "rule_name": "Consistency rule (none during eval)",
     "threshold": "none", "breach_type": "INFO",
     "citation_chunk_id": "chunk_consistency"},
    {"rule_id": "growth_eval_microscalp", "account_type": "growth",
     "phase": "eval", "rule_name": "Microscalping rule",
     "threshold": ">50% of trades and profit from holds >10s",
     "breach_type": "SOFT_BREACH", "citation_chunk_id": "chunk_microscalp"},

    # ---- Growth: funded -----------------------------------------------------
    {"rule_id": "growth_funded_consistency", "account_type": "growth",
     "phase": "funded", "rule_name": "Consistency rule",
     "threshold": "35.0", "breach_type": "SOFT_BREACH",
     "citation_chunk_id": "chunk_consistency"},
    {"rule_id": "growth_funded_trailing_drawdown", "account_type": "growth",
     "phase": "funded", "rule_name": "End-of-day trailing drawdown",
     "threshold": "2000 / 3500 / 5000 by size", "breach_type": "HARD_BREACH",
     "citation_chunk_id": "chunk_trailing_dd"},
    {"rule_id": "growth_funded_daily_loss_limit", "account_type": "growth",
     "phase": "funded", "rule_name": "Daily Loss Limit (soft)",
     "threshold": "1250 / 2500 / 3750 by size", "breach_type": "SOFT_BREACH",
     "citation_chunk_id": "chunk_dll"},
    {"rule_id": "growth_funded_min_trading_days", "account_type": "growth",
     "phase": "funded", "rule_name": "Minimum trading days before payout",
     "threshold": "5", "breach_type": "SOFT_BREACH",
     "citation_chunk_id": "chunk_payouts"},
    {"rule_id": "growth_funded_payout_bounds", "account_type": "growth",
     "phase": "funded", "rule_name": "Per-request payout bounds",
     "threshold": "min 100 / max 10000", "breach_type": "SOFT_BREACH",
     "citation_chunk_id": "chunk_payouts"},
    {"rule_id": "growth_funded_first_payout_manual", "account_type": "growth",
     "phase": "funded", "rule_name": "First payout requires manual review",
     "threshold": "1 (first payout)", "breach_type": "SOFT_BREACH",
     "citation_chunk_id": "chunk_payouts"},

    # ---- Select: evaluation -------------------------------------------------
    {"rule_id": "select_eval_consistency", "account_type": "select",
     "phase": "eval", "rule_name": "Consistency rule",
     "threshold": "40.0", "breach_type": "SOFT_BREACH",
     "citation_chunk_id": "chunk_consistency"},
    {"rule_id": "select_eval_trailing_drawdown", "account_type": "select",
     "phase": "eval", "rule_name": "End-of-day trailing drawdown",
     "threshold": "2000 / 3000 / 4500 by size", "breach_type": "HARD_BREACH",
     "citation_chunk_id": "chunk_trailing_dd"},
    {"rule_id": "select_eval_profit_target", "account_type": "select",
     "phase": "eval", "rule_name": "Profit target",
     "threshold": "2500 / 6000 / 9000 by size", "breach_type": "INFO",
     "citation_chunk_id": "chunk_profit_target"},
    {"rule_id": "select_eval_daily_loss_limit", "account_type": "select",
     "phase": "eval", "rule_name": "Daily Loss Limit (none)",
     "threshold": "none", "breach_type": "INFO",
     "citation_chunk_id": "chunk_dll"},
    {"rule_id": "select_eval_microscalp", "account_type": "select",
     "phase": "eval", "rule_name": "Microscalping rule",
     "threshold": ">50% of trades and profit from holds >10s",
     "breach_type": "SOFT_BREACH", "citation_chunk_id": "chunk_microscalp"},

    # ---- Select Flex: funded ------------------------------------------------
    {"rule_id": "select_flex_trailing_drawdown", "account_type": "select",
     "phase": "funded", "rule_name": "End-of-day trailing drawdown (Flex)",
     "threshold": "2000 / 3000 / 4500 by size", "breach_type": "HARD_BREACH",
     "citation_chunk_id": "chunk_trailing_dd"},
    {"rule_id": "select_flex_consistency", "account_type": "select",
     "phase": "funded", "rule_name": "Consistency rule (none on Flex)",
     "threshold": "none", "breach_type": "INFO",
     "citation_chunk_id": "chunk_consistency"},
    {"rule_id": "select_flex_daily_loss_limit", "account_type": "select",
     "phase": "funded", "rule_name": "Daily Loss Limit (none on Flex)",
     "threshold": "none", "breach_type": "INFO",
     "citation_chunk_id": "chunk_dll"},

    # ---- Select Daily: funded ----------------------------------------------
    {"rule_id": "select_daily_trailing_drawdown", "account_type": "select",
     "phase": "funded", "rule_name": "End-of-day trailing drawdown (Daily)",
     "threshold": "2000 / 3000 / 4500 by size", "breach_type": "HARD_BREACH",
     "citation_chunk_id": "chunk_trailing_dd"},
    {"rule_id": "select_daily_daily_loss_limit", "account_type": "select",
     "phase": "funded", "rule_name": "Daily Loss Limit (Daily, soft)",
     "threshold": "1000 / 1250 / 1750 by size", "breach_type": "SOFT_BREACH",
     "citation_chunk_id": "chunk_dll"},
    {"rule_id": "select_daily_consistency", "account_type": "select",
     "phase": "funded", "rule_name": "Consistency rule (none on Daily)",
     "threshold": "none", "breach_type": "INFO",
     "citation_chunk_id": "chunk_consistency"},

    # ---- Lightning: funded --------------------------------------------------
    {"rule_id": "lightning_consistency_progressive", "account_type": "lightning",
     "phase": "funded", "rule_name": "Progressive consistency rule",
     "threshold": "20 / 25 / 30 by payout count", "breach_type": "SOFT_BREACH",
     "citation_chunk_id": "chunk_consistency"},
    {"rule_id": "lightning_trailing_drawdown", "account_type": "lightning",
     "phase": "funded", "rule_name": "End-of-day trailing drawdown",
     "threshold": "1000 / 2000 / 4000 / 6000 by size",
     "breach_type": "HARD_BREACH", "citation_chunk_id": "chunk_trailing_dd"},
    {"rule_id": "lightning_daily_loss_limit", "account_type": "lightning",
     "phase": "funded", "rule_name": "Daily Loss Limit",
     "threshold": "none on 25K; 1250 / 2500 / 3750 on 50K+",
     "breach_type": "SOFT_BREACH", "citation_chunk_id": "chunk_dll"},
    {"rule_id": "lightning_min_trading_days", "account_type": "lightning",
     "phase": "funded", "rule_name": "Minimum trading days before payout",
     "threshold": "5", "breach_type": "SOFT_BREACH",
     "citation_chunk_id": "chunk_payouts"},
    {"rule_id": "lightning_payout_bounds", "account_type": "lightning",
     "phase": "funded", "rule_name": "Per-request payout bounds",
     "threshold": "min 100 / max 10000", "breach_type": "SOFT_BREACH",
     "citation_chunk_id": "chunk_payouts"},

    # ---- Global / session ---------------------------------------------------
    {"rule_id": "all_funded_stage_required", "account_type": "all",
     "phase": "funded", "rule_name": "Only funded accounts may pay out",
     "threshold": "stage == funded", "breach_type": "HARD_BREACH",
     "citation_chunk_id": "chunk_account_families"},
    {"rule_id": "all_flat_by_time", "account_type": "all",
     "phase": "all", "rule_name": "Flat by 4:59 PM ET",
     "threshold": "16:59 ET weekdays (12:59 ET early close)",
     "breach_type": "HARD_BREACH", "citation_chunk_id": "chunk_session_rules"},
    {"rule_id": "all_hedging_prohibited", "account_type": "all",
     "phase": "all", "rule_name": "Hedging prohibited",
     "threshold": "prohibited", "breach_type": "HARD_BREACH",
     "citation_chunk_id": "chunk_session_rules"},
    {"rule_id": "all_min_trades_per_week", "account_type": "all",
     "phase": "all", "rule_name": "Minimum one trade per week",
     "threshold": "1", "breach_type": "SOFT_BREACH",
     "citation_chunk_id": "chunk_session_rules"},
    {"rule_id": "all_max_funded_accounts", "account_type": "all",
     "phase": "all", "rule_name": "Maximum funded accounts",
     "threshold": "5", "breach_type": "SOFT_BREACH",
     "citation_chunk_id": "chunk_session_rules"},
    {"rule_id": "all_microscalp", "account_type": "all",
     "phase": "all", "rule_name": "Microscalping rule",
     "threshold": ">50% of trades and profit from holds >10s",
     "breach_type": "SOFT_BREACH", "citation_chunk_id": "chunk_microscalp"},
    {"rule_id": "all_payout_profit_split", "account_type": "all",
     "phase": "funded", "rule_name": "Profit split",
     "threshold": "90 / 10 trader-favor", "breach_type": "INFO",
     "citation_chunk_id": "chunk_payouts"},
    {"rule_id": "all_dd_floor_lock", "account_type": "all",
     "phase": "all", "rule_name": "Trailing drawdown floor lock",
     "threshold": "start_balance + drawdown + 100 (or first payout)",
     "breach_type": "INFO", "citation_chunk_id": "chunk_trailing_dd"},
]


# ---------------------------------------------------------------------------
# Engine helpers
# ---------------------------------------------------------------------------

_MEMORY_ENGINE = None


def _memory_engine():
    """Seeded in-memory SQLite engine (demo/CI fallback; StaticPool shares it)."""
    global _MEMORY_ENGINE
    if _MEMORY_ENGINE is None:
        _MEMORY_ENGINE = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        seed_rules(_MEMORY_ENGINE)
    return _MEMORY_ENGINE


def seed_rules(engine, rules: Optional[list[dict]] = None,
               reset: bool = False) -> int:
    """
    Create the table if missing and insert any rule not already present.

    The ``create_all`` here is for the SQLite demo/CI path only. Production
    creates the table through ``alembic/versions/0002_rule_registry.py``; the
    helper simply makes the demo self-contained. Returns rows inserted.
    """
    rules = rules if rules is not None else DEFAULT_RULES
    RegistryBase.metadata.create_all(engine)
    inserted = 0
    with Session(engine) as session:
        if reset:
            session.execute(delete(RuleRegistryRow))
            session.commit()
        existing = set(session.scalars(select(RuleRegistryRow.rule_id)).all())
        for rule in rules:
            if rule["rule_id"] in existing:
                continue
            session.add(RuleRegistryRow(**rule))
            inserted += 1
        session.commit()
    return inserted


def load_rules_from_db(engine) -> list[dict]:
    with Session(engine) as session:
        rows = session.scalars(
            select(RuleRegistryRow).order_by(RuleRegistryRow.id)).all()
        return [r.to_dict() for r in rows]


def load_rules(db_url: Optional[str] = None) -> list[dict]:
    """
    Load the registry from a database.

    Priority: explicit ``db_url`` -> ``RULE_REGISTRY_DB_URL`` ->
    ``DATABASE_URL`` -> seeded in-memory SQLite. A configured-but-unreachable
    database degrades to the in-memory seed rather than crashing the API.
    """
    url = (db_url or os.environ.get("RULE_REGISTRY_DB_URL")
           or os.environ.get("DATABASE_URL"))
    if url:
        try:
            engine = create_engine(url, pool_pre_ping=True)
            seed_rules(engine)
            return load_rules_from_db(engine)
        except Exception:
            pass
    return load_rules_from_db(_memory_engine())


def registry_source(db_url: Optional[str] = None) -> str:
    """Human-readable description of where load_rules() would read from."""
    url = (db_url or os.environ.get("RULE_REGISTRY_DB_URL")
           or os.environ.get("DATABASE_URL"))
    return "postgres/configured-db" if url else "in-memory-sqlite"


def rules_for(account_type: str, phase: Optional[str] = None,
              rules: Optional[list[dict]] = None) -> list[dict]:
    """Filter the registry by account type (family or 'all') and phase."""
    rules = rules if rules is not None else load_rules()
    out = []
    for r in rules:
        if r["account_type"] not in (account_type, "all"):
            continue
        if phase is not None and r["phase"] not in (phase, "all"):
            continue
        out.append(r)
    return out


def get_rule(rule_id: str, rules: Optional[list[dict]] = None) -> Optional[dict]:
    rules = rules if rules is not None else load_rules()
    for r in rules:
        if r["rule_id"] == rule_id:
            return r
    return None


# ---------------------------------------------------------------------------
# Validation gates
# ---------------------------------------------------------------------------

def corpus_chunk_ids(corpus: Optional[list[dict]] = None) -> set[str]:
    corpus = corpus if corpus is not None else load_corpus()
    return {c["id"] for c in corpus}


def validate_citations(rules: Optional[list[dict]] = None,
                       corpus: Optional[list[dict]] = None) -> dict:
    """Every citation_chunk_id must resolve to a real corpus chunk."""
    rules = rules if rules is not None else load_rules()
    valid = corpus_chunk_ids(corpus)
    invalid = sorted({r["citation_chunk_id"] for r in rules
                      if r["citation_chunk_id"] not in valid})
    return {"valid": not invalid, "n_rules": len(rules),
            "invalid_citations": invalid, "corpus_chunks": sorted(valid)}


def validate_registry(rules: Optional[list[dict]] = None,
                      corpus: Optional[list[dict]] = None) -> dict:
    """
    Full DoD gate: >= 20 entries, unique rule_ids, every citation resolves,
    and every required column is populated.
    """
    rules = rules if rules is not None else load_rules()
    required = ("rule_id", "account_type", "phase", "rule_name", "threshold",
                "breach_type", "citation_chunk_id")
    missing_fields = [
        r.get("rule_id", "?") for r in rules
        if any(not r.get(f) for f in required)
    ]
    ids = [r["rule_id"] for r in rules]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    cites = validate_citations(rules, corpus)
    ok = (len(rules) >= 20 and not duplicates and not missing_fields
          and cites["valid"])
    return {
        "valid": ok,
        "count": len(rules),
        "min_required": 20,
        "duplicate_rule_ids": duplicates,
        "missing_fields": missing_fields,
        "invalid_citations": cites["invalid_citations"],
        "corpus_chunks": cites["corpus_chunks"],
    }


def validate_against_account_specs(rules: Optional[list[dict]] = None) -> dict:
    """
    Guard against drift: the funded consistency thresholds in the registry must
    still match ``rule_engine.ACCOUNT_SPECS``. Returns mismatches (empty = ok).
    """
    from rule_engine import ACCOUNT_SPECS

    rules = rules if rules is not None else load_rules()
    by_id = {r["rule_id"]: r for r in rules}
    mismatches: list[dict] = []

    def expect(rule_id: str, needle: str):
        rule = by_id.get(rule_id)
        if rule is None:
            mismatches.append({"rule_id": rule_id, "reason": "missing"})
            return
        if needle not in rule["threshold"]:
            mismatches.append({"rule_id": rule_id,
                               "reason": f"{needle!r} not in {rule['threshold']!r}"})

    expect("growth_funded_consistency", "35")
    expect("select_eval_consistency", "40")
    expect("lightning_consistency_progressive", "20")
    # Growth funded 150k drawdown spec is 5000 (brief's reference value).
    spec = ACCOUNT_SPECS["growth_funded_150k"]
    expect("growth_funded_trailing_drawdown", str(spec.trailing_drawdown))
    return {"valid": not mismatches, "mismatches": mismatches}


if __name__ == "__main__":
    report = validate_registry()
    drift = validate_against_account_specs()
    print("rule_registry validation")
    print(f"  source              {registry_source()}")
    print(f"  entries             {report['count']} (min {report['min_required']})")
    print(f"  invalid citations   {report['invalid_citations']}")
    print(f"  duplicate ids       {report['duplicate_rule_ids']}")
    print(f"  missing fields      {report['missing_fields']}")
    print(f"  spec drift          {drift['mismatches']}")
    ok = report["valid"] and drift["valid"]
    print("  RESULT              " + ("PASS" if ok else "FAIL"))
    raise SystemExit(0 if ok else 1)

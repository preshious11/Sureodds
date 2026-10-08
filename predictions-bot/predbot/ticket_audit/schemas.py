"""Detached audit values and strict extraction; no production dependencies."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import unicodedata
from .resources import ResourceLimit, validate_payload

VERDICTS = ('STRONG', 'REASONABLE', 'RISKY', 'VERY_RISKY', 'INSUFFICIENT_EVIDENCE')
LEVELS = ('STRONG_LOCAL', 'GOOD_LOCAL', 'RESEARCHED', 'LIMITED', 'INSUFFICIENT')
SCHEMA_VERSION = 'audit-1'


class AuditUnavailable(Exception):
    """Fixed, non-sensitive reason codes only; never attach transport exceptions."""


class TicketResourceLimit(AuditUnavailable):
    """Technical payload/depth/work limit, never a product selection-count limit."""


def instant(value):
    if not value:
        return None
    try:
        d = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return d.astimezone(timezone.utc) if d.tzinfo else None
    except (ValueError, TypeError, OverflowError):
        return None


def normalized(value):
    # Preserve all words, age/women/reserve markers and diacritics. No team aliases or fuzzy matching.
    return ' '.join(unicodedata.normalize('NFKC', value if isinstance(value, str) else '').casefold().split())


def number(value):
    if value is None:
        return None
    try:
        n = Decimal(str(value))
        return n if n.is_finite() else None
    except InvalidOperation:
        return None


@dataclass(frozen=True)
class NormalizedSelection:
    sport: str | None = None
    competition: str | None = None
    home: str | None = None
    away: str | None = None
    kickoff: str | None = None
    market_family: str | None = None
    selection: str | None = None
    line: float | None = None
    submitted_odds: float | None = None
    source_event_id: str | None = None
    raw_text: str | None = None
    canonical_event_id: str | None = None
    period: str | None = None
    stat: str | None = None
    entity: str | None = None

    def coordinates(self):
        """Only minimal sports coordinates leave the application; raw text/IDs of users never do."""
        return {f.name: getattr(self, f.name) for f in fields(self)
                if f.name not in ('raw_text', 'submitted_odds', 'source_event_id')}

    def researchable(self):
        return bool(all((self.sport, self.competition, self.home, self.away, instant(self.kickoff),
                         self.market_family, self.selection, self.period, self.stat, self.entity))
                    and (self.market_family not in ('total', 'handicap') or number(self.line) is not None))


@dataclass(frozen=True)
class NormalizedTicket:
    input_type: str
    source_bookmaker: str | None
    source_code: str | None
    submitted_combined_odds: float | None
    selections: tuple[NormalizedSelection, ...]
    extraction_confidence: str
    extraction_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class LocalEvidenceMatch:
    status: str  # EXACT / MISSING / AMBIGUOUS / UNAVAILABLE
    record: dict | None = None
    metrics: dict = field(default_factory=dict)
    provenance: tuple[str, ...] = ()
    evaluated_at: str | None = None
    limitations: tuple[str, ...] = ()


@dataclass(frozen=True)
class ResearchFact:
    category: str
    claim: str
    value: str | float | None
    source_title: str
    source_url: str
    observed_at: str | None
    source_quality: str
    event_date: str | None = None
    freshness: str = 'UNASSESSED'
    source: str | None = None


@dataclass(frozen=True)
class ResearchEvidence:
    fixture: dict
    selection: dict
    facts: tuple[ResearchFact, ...]
    interpretations: tuple[str, ...]
    limitations: tuple[str, ...]
    research_confidence: str
    searched_at: str
    model: str
    grounding_metadata: dict
    verdict: str = 'INSUFFICIENT_EVIDENCE'
    schema_version: str = SCHEMA_VERSION
    retrieval_metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FixtureEvidence:
    identity: dict
    sport: str | None
    kickoff: str | None
    evidence_source: str
    local: LocalEvidenceMatch | None
    research: ResearchEvidence | None
    freshness: str
    completeness: str
    limitations: tuple[str, ...]
    provenance: tuple[str, ...]


@dataclass(frozen=True)
class SelectionAudit:
    normalized_selection: NormalizedSelection
    verdict: str
    evidence_level: str
    local_probability: float | None
    market_probability: float | None
    strengths: tuple[str, ...]
    concerns: tuple[str, ...]
    key_facts: tuple[ResearchFact, ...]
    limitations: tuple[str, ...]
    safer_alternative: dict | None
    provenance: tuple[str, ...]
    evidence: FixtureEvidence


@dataclass(frozen=True)
class TicketAudit:
    selections: tuple[SelectionAudit, ...]
    strongest_selections: tuple[int, ...]
    weakest_selections: tuple[int, ...]
    overall_verdict: str
    evidence_summary: dict
    limitations: tuple[str, ...]
    generated_at: str


def _strict_object(value, allowed, required):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise AuditUnavailable('malformed_structured_output')


def ticket_from_json(value):
    """Reliable deterministic adapter for our JSON contract, not a booking-code decoder."""
    try:
        value = validate_payload(value)
        names = {f.name for f in fields(NormalizedTicket)}
        _strict_object(value, names, names)
        if value['input_type'] not in ('text', 'image', 'normalized') or value['extraction_confidence'] not in ('HIGH', 'MEDIUM', 'LOW'):
            raise ValueError
        if not isinstance(value['selections'], list) or not value['selections']:
            raise ValueError
        selections = []
        snames = {f.name for f in fields(NormalizedSelection)}
        for s in value['selections']:
            _strict_object(s, snames, snames)
            for name, v in s.items():
                if name in ('line', 'submitted_odds'):
                    if v is not None and (isinstance(v, bool) or not isinstance(v, (float, int)) or not math.isfinite(v)):
                        raise ValueError
                elif v is not None and (not isinstance(v, str) or len(v) > 2000):
                    raise ValueError
            if s['kickoff'] is not None and instant(s['kickoff']) is None:
                raise ValueError
            selections.append(NormalizedSelection(**s))
        for k in ('source_bookmaker', 'source_code'):
            if value[k] is not None and (not isinstance(value[k], str) or len(value[k]) > 200):
                raise ValueError
        odds = value['submitted_combined_odds']
        if odds is not None and (isinstance(odds, bool) or not isinstance(odds, (int, float)) or not math.isfinite(odds) or odds <= 1):
            raise ValueError
        warnings = value['extraction_warnings']
        if not isinstance(warnings, list) or any(not isinstance(w, str) or len(w) > 2000 for w in warnings):
            raise ValueError
        return NormalizedTicket(**{**value, 'selections': tuple(selections), 'extraction_warnings': tuple(warnings)})
    except ResourceLimit as exc:
        raise TicketResourceLimit(str(exc)) from None
    except (ValueError, TypeError, KeyError):
        raise AuditUnavailable('malformed_structured_output') from None


def ticket_json_schema():
    props = {f.name: {'type': ['number' if f.name in ('line', 'submitted_odds') else 'string', 'null']}
             for f in fields(NormalizedSelection)}
    return {'type': 'object', 'additionalProperties': False,
            'properties': {
                'input_type': {'type': 'string', 'enum': ['text', 'image', 'normalized']},
                'source_bookmaker': {'type': ['string', 'null']}, 'source_code': {'type': ['string', 'null']},
                'submitted_combined_odds': {'type': ['number', 'null']},
                # The complete nullable item schema + maxItems is rejected by
                # 3.5 Flash-Lite. Resource bounds are enforced outside the model's
                # constrained decoder; ticket size is not a product restriction.
                'selections': {'type': 'array', 'minItems': 1, 'items': {
                    'type': 'object', 'additionalProperties': False, 'properties': props, 'required': list(props)}},
                'extraction_confidence': {'type': 'string', 'enum': ['HIGH', 'MEDIUM', 'LOW']},
                'extraction_warnings': {'type': 'array', 'items': {'type': 'string'}}},
            'required': [f.name for f in fields(NormalizedTicket)]}

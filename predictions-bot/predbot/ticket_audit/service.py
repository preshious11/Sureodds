"""Analysis-only orchestration. Hard three-uncached-leg budget; no Telegram or tracking."""
from collections import Counter
from dataclasses import replace
from datetime import timedelta
import logging

from .research import research_level
from .schemas import (AuditUnavailable, FixtureEvidence, LocalEvidenceMatch, SelectionAudit, TicketAudit,
                      TicketResourceLimit, instant, number)
from .resources import ResourceLimit, selection_batches

log = logging.getLogger(__name__)


def local_level(match, now):
    if match.status != 'EXACT':
        return 'INSUFFICIENT'
    metrics = match.metrics
    at = instant(match.evaluated_at)
    if not at or now < at or now - at > timedelta(hours=6) or match.limitations:
        return 'LIMITED'
    valid_p = lambda v: number(v) is not None and 0 <= number(v) <= 1
    if not all(valid_p(metrics.get(k)) for k in ('p_model', 'p_market')):
        return 'LIMITED'
    if (valid_p(metrics.get('p_independent')) and metrics.get('p_model_basis') == 'independent'
            and number(metrics.get('bookmaker_count')) is not None and number(metrics['bookmaker_count']) >= 3
            and metrics.get('fair_books') and number(metrics.get('edge_ev')) is not None):
        return 'STRONG_LOCAL'
    return 'GOOD_LOCAL'


class AuditService:
    def __init__(self, resolver, researcher=None, max_uncached_research=3, analyzer=None):
        self.resolver, self.researcher = resolver, researcher
        self.analyzer = analyzer
        if not 0 <= max_uncached_research <= 3:
            raise ValueError('maximum three uncached researches per ticket')
        self.max_uncached_research = max_uncached_research

    def audit(self, ticket, now):
        if not ticket.selections or instant(now) is None:
            raise AuditUnavailable('invalid_ticket')
        # Read ALL local legs before considering remote research.
        matches, local_batches = [], 0
        try:
            for batch in selection_batches(ticket.selections):
                local_batches += 1
                for selection in batch:
                    try:
                        matches.append(self.resolver.resolve(selection, now))
                    except Exception:
                        matches.append(LocalEvidenceMatch('UNAVAILABLE', limitations=('Local evidence unavailable',)))
        except ResourceLimit as exc:
            raise TicketResourceLimit(str(exc)) from None
        research = [None] * len(matches)
        limits = [[] for _ in matches]
        budget = self.max_uncached_research
        research_operations = 0
        failed_cache = set()
        def cached(selection, i):
            try:
                value = self.researcher.cached(selection, now)
                if value:
                    self.researcher.count_cache_hit(now)
                return value
            except Exception:
                limits[i].append('Cached research unavailable')
                failed_cache.add(i)
                return None
        if self.researcher:
            for i, (selection, match) in enumerate(zip(ticket.selections, matches)):
                if match.status == 'EXACT' or match.status == 'AMBIGUOUS' or not selection.researchable():
                    continue
                research[i] = cached(selection, i)
            for i, (selection, match) in enumerate(zip(ticket.selections, matches)):
                if match.status == 'EXACT' or research[i] or match.status == 'AMBIGUOUS' or not selection.researchable() or i in failed_cache:
                    continue
                # Earlier legs may just have populated shared fixture evidence.
                research[i] = cached(selection, i)
                if research[i]:
                    continue
                if i in failed_cache:
                    continue
                # A leg answerable from already-downloaded public data costs no request,
                # so it does not spend a slot (keeps large same-dataset tickets researchable).
                offline = getattr(type(self.researcher), 'offline', None)
                try:
                    free = bool(offline and offline(self.researcher, selection, now))
                except Exception:
                    limits[i].append('Research availability could not be established')
                    continue
                if budget <= 0 and not free:
                    limits[i].append('Per-ticket research budget exhausted')
                    continue
                if not free:
                    budget -= 1  # Failures consume a slot as well; no retry/fallback.
                    research_operations += 1
                try:
                    research[i] = self.researcher.research(selection, now)
                except AuditUnavailable as exc:
                    # Owned researchers use fixed, non-sensitive reason codes.
                    limits[i].append(str(exc))
                except Exception:
                    limits[i].append('Research unavailable for this selection')
        audits = []
        for i, (selection, match) in enumerate(zip(ticket.selections, matches)):
            re = research[i]
            level = local_level(match, now) if match.status == 'EXACT' else research_level(re, now)
            limitations = tuple(match.limitations) + tuple(limits[i]) + (re.limitations if re else ())
            p = match.metrics.get('p_model') if match.status == 'EXACT' else None
            pm = match.metrics.get('p_market') if match.status == 'EXACT' else None
            # Strength of evidence is not a high win-probability claim. Conservative provisional verdicts.
            verdict = 'INSUFFICIENT_EVIDENCE'
            if level in ('GOOD_LOCAL', 'STRONG_LOCAL'):
                ev = number(match.metrics.get('ev'))
                verdict = 'REASONABLE' if ev is not None and ev >= 0 else 'RISKY'
            elif level == 'RESEARCHED':
                verdict = re.verdict
            provenance = match.provenance + (tuple(dict.fromkeys(f.source_url for f in re.facts)) if re else ())
            source = 'LOCAL' if match.status == 'EXACT' else 'RESEARCH' if re else 'LOCAL'
            evidence = FixtureEvidence(selection.coordinates(), selection.sport, selection.kickoff, source,
                         match, re, 'FRESH' if level in ('STRONG_LOCAL', 'GOOD_LOCAL', 'RESEARCHED') else 'LIMITED',
                         level, limitations, provenance)
            audits.append(SelectionAudit(selection, verdict, level, p, pm,
                         ('Exact previously evaluated local selection',) if level in ('GOOD_LOCAL', 'STRONG_LOCAL') else (),
                         ('Qualitative research; no model probability',) if re else (),
                         re.facts if re else (), limitations, None, provenance, evidence))
        # Foundation deliberately does not rank incomparable probabilities or fabricate an accumulator probability.
        strongest = tuple(i for i, a in enumerate(audits) if a.evidence_level == 'STRONG_LOCAL')
        weakest = tuple(i for i, a in enumerate(audits) if a.evidence_level in ('LIMITED', 'INSUFFICIENT'))
        usable = any(a.evidence_level in ('GOOD_LOCAL', 'STRONG_LOCAL', 'RESEARCHED') for a in audits)
        overall = ('INSUFFICIENT_EVIDENCE' if not usable else 'RISKY' if weakest or
                   any(a.verdict in ('RISKY', 'VERY_RISKY') for a in audits) else 'REASONABLE')
        ticket_limits = ('Analysis only; no tracking or accumulator probability. Provisional evidence policy.',)
        if weakest:
            ticket_limits += ('Some legs have limited/insufficient evidence; usable leg assessments remain independent.',)
        result = TicketAudit(tuple(audits), strongest, weakest, overall,
                           dict(Counter(a.evidence_level for a in audits)),
                           ticket_limits, now.isoformat())
        if self.analyzer:
            try:
                result = self.analyzer.analyze_ticket(result, now)
            except AuditUnavailable as exc:
                result = replace(result,
                    limitations=result.limitations + ('Gemini analysis unavailable: ' + str(exc),))
        log.info('Ticket Audit processing | selection_count=%s | local_batch_count=%s | uncached_research_operation_count=%s',
                 len(result.selections), local_batches, research_operations)
        return result

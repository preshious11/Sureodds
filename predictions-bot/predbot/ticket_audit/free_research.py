"""Explicitly injected public-dataset research; no sports clients, search or LLM.

Only the source below has an implemented, reviewed access policy. The dataset
owner documents raw downloads and CC0 reuse. Never fetch URLs from ticket text.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time
from typing import Protocol

import requests
from predbot import ticket_audit_switch

from .research import (FIXTURE_FIELDS, SELECTION_FIELDS, ResearchCache,
                       research_key, research_level)
from .schemas import (AuditUnavailable, ResearchEvidence, ResearchFact,
                      instant, normalized, number)


class ResearchSourceAdapter(Protocol):
    source_id: str

    def supports(self, selection): ...
    def fetch_fixture(self, selection, now, cache): ...
    def market_evidence(self, fixture, selection, now): ...


class PublicSourceBudget:
    """Separate, durable source budget; never use the production/Google ledger.

    Failed requests consume quota. One shared ledger should serve every resolver
    instance; UTC days, 20/source/day and 3/source/hour by default.
    """
    def __init__(self, path, daily=20, hourly=3):
        if not 1 <= daily <= 20 or not 1 <= hourly <= 3:
            raise ValueError('invalid public source budget')
        self.path, self.daily, self.hourly = Path(path), daily, hourly
        with sqlite3.connect(self.path) as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if names - {'audit_source_usage'}:
                raise AuditUnavailable('unrelated_source_usage_database')
            db.execute('CREATE TABLE IF NOT EXISTS audit_source_usage '
                       '(source TEXT, hour TEXT, requests INTEGER, PRIMARY KEY(source,hour))')

    def reserve(self, source, now):
        at = instant(now)
        if at is None:
            raise AuditUnavailable('invalid_research_time')
        hour, day = at.strftime('%Y-%m-%dT%H'), at.strftime('%Y-%m-%d')
        try:
            with sqlite3.connect(self.path, timeout=5) as db:
                db.execute('BEGIN IMMEDIATE')
                count = db.execute('SELECT COALESCE(SUM(requests),0) FROM audit_source_usage '
                                   'WHERE source=? AND hour LIKE ?', (source, day + '%')).fetchone()[0]
                used = db.execute('SELECT requests FROM audit_source_usage WHERE source=? AND hour=?',
                                  (source, hour)).fetchone()
                if count >= self.daily or (used and used[0] >= self.hourly):
                    raise AuditUnavailable('public_source_budget_exhausted')
                db.execute('INSERT INTO audit_source_usage VALUES (?,?,1) '
                           'ON CONFLICT(source,hour) DO UPDATE SET requests=requests+1', (source, hour))
        except sqlite3.Error:
            raise AuditUnavailable('public_source_budget_unavailable') from None


class OpenFootballAdapter:
    """EPL senior men's full-match scores only; exact names, no alias/fuzzy match.

    Date-only fixture identity is retained as a LIMITATION, never promoted to an
    exact local match. A unique same pair within UTC-date +/-1 day is required:
    the source's clock has no time zone. Exclude same-day results conservatively.
    """
    source_id = 'openfootball-epl-v1'
    license_url = 'https://raw.githubusercontent.com/openfootball/football.json/master/LICENSE.md'
    base = 'https://raw.githubusercontent.com/openfootball/football.json/master/'
    competitions = ('england premier league', 'english premier league')
    markets = {'result3': {'home', 'draw', 'away'}, 'dc': {'1x', 'x2', '12'},
               'dnb': {'home', 'away'}, 'total': {'over', 'under'},
               'btts': {'yes', 'no'}, 'handicap': {'home', 'away'}}

    def __init__(self, budget, transport=None):
        self.budget = budget
        self.transport = transport or requests.Session()

    def supports(self, s):
        return (s.sport == 'football' and normalized(s.competition) in self.competitions
                and s.period == 'full' and s.stat in ('goals', 'points') and s.entity == 'all'
                and s.market_family in self.markets and s.selection in self.markets[s.market_family]
                and (s.market_family not in ('total', 'handicap') or number(s.line) is not None))

    def url(self, selection):
        kickoff = instant(selection.kickoff)
        year = kickoff.year - (kickoff.month < 7)
        return self.base + f'{year}-{str(year + 1)[2:]}/en.1.json'

    def offline(self, selection, now, cache):
        """True when this fixture can be answered from an already-cached dataset (no HTTP)."""
        return cache.peek('dataset:' + self.url(selection), now) is not None

    def _download(self, url, now):
        if not ticket_audit_switch.ENABLED:
            raise AuditUnavailable('ticket_audit_disabled')
        if not re.fullmatch(re.escape(self.base) + r'20\d{2}-\d{2}/en\.1\.json', url):
            raise AuditUnavailable('unreviewed_public_source')
        self.budget.reserve(self.source_id, now)  # BEFORE HTTP; failures consume quota.
        response = None
        try:
            started = time.monotonic()
            response = self.transport.get(url, timeout=(5, 10), allow_redirects=False, stream=True,
                headers={'User-Agent': 'Sureodds-TicketAudit/1.0', 'Accept': 'application/json'})
            if response.status_code != 200:
                raise AuditUnavailable('public_source_unavailable')
            body = bytearray()
            # Yield even a slow trickle, rather than waiting indefinitely to fill
            # a large chunk while each socket read keeps resetting its timeout.
            for chunk in response.iter_content(chunk_size=1):
                if time.monotonic() - started > 20:
                    raise AuditUnavailable('public_source_timeout')
                body.extend(chunk)
                if len(body) > 1_000_000:
                    raise AuditUnavailable('public_source_too_large')
            data = json.loads(body)
            if (not isinstance(data, dict) or not isinstance(data.get('matches'), list)
                    or not 1 <= len(data['matches']) <= 1000
                    or 'premier league' not in normalized(data.get('name'))):
                raise AuditUnavailable('malformed_public_source')
            rows = []
            for row in data['matches']:
                if not isinstance(row, dict):
                    raise AuditUnavailable('malformed_public_source')
                date.fromisoformat(row['date'])
                if any(not isinstance(row.get(k), str) or not 1 <= len(row[k]) <= 150 for k in ('team1', 'team2')):
                    raise AuditUnavailable('malformed_public_source')
                # Current releases mix old {ft:[h,a]} and direct [h,a]
                # representations. Both mean the published full-time score.
                raw_score = row.get('score')
                score = raw_score.get('ft') if isinstance(raw_score, dict) else raw_score
                if score is not None and (not isinstance(score, list) or len(score) != 2 or
                        any(type(v) is not int or not 0 <= v <= 30 for v in score)):
                    raise AuditUnavailable('malformed_public_source')
                rows.append({'date': row['date'], 'home': row['team1'], 'away': row['team2'], 'score': score})
            return rows, hashlib.sha256(body).hexdigest()
        except requests.Timeout:
            raise AuditUnavailable('public_source_timeout') from None
        except (requests.RequestException, ValueError, KeyError, TypeError, AttributeError):
            raise AuditUnavailable('malformed_or_unavailable_public_source') from None
        finally:
            if response is not None:
                response.close()

    def fetch_fixture(self, s, now, cache):
        url = self.url(s)
        coords = s.coordinates()
        def load_dataset():
            rows, digest = self._download(url, now)
            return ResearchEvidence({k: coords[k] for k in FIXTURE_FIELDS}, {}, (), (), (), 'UNASSESSED',
                now.isoformat(), self.source_id, {}, retrieval_metadata={'rows': rows, 'sha256': digest, 'source_url': url})
        # Reuse the SAME bounded cache implementation for dataset, fixture, market.
        # Dataset expiration follows its first requested fixture, so never extends
        # past that kickoff; no fabricated clock or source freshness timestamp.
        dataset = cache.get_or_load('dataset:' + url, now, load_dataset)
        rows = dataset.retrieval_metadata['rows']
        day = instant(s.kickoff).date()
        matches = [r for r in rows if normalized(r['home']) == normalized(s.home)
                   and normalized(r['away']) == normalized(s.away)
                   and abs((date.fromisoformat(r['date']) - day).days) <= 1]
        if len(matches) != 1:
            raise AuditUnavailable('public_fixture_ambiguous' if matches else 'public_fixture_missing')
        # Any disagreeing duplicate score record is a conflict, not a majority vote.
        unique = {}
        for row in rows:
            key = row['date'], normalized(row['home']), normalized(row['away'])
            if key in unique and unique[key]['score'] != row['score']:
                raise AuditUnavailable('conflicting_public_results')
            unique[key] = row
        return replace(dataset, fixture={k: coords[k] for k in FIXTURE_FIELDS},
            limitations=('Community dataset; independent/official corroboration unavailable',
                         'Fixture identity uses date and exact names; source kickoff timezone UNKNOWN',
                         'Upstream update time UNKNOWN; no guarantee of current completeness',
                         'Injury/lineup availability UNKNOWN; opponent strength not assessed'),
            retrieval_metadata={**dataset.retrieval_metadata, 'rows': list(unique.values())})

    def market_evidence(self, fixture, s, now):
        rows = fixture.retrieval_metadata['rows']
        # Date-only records may represent any time zone: exclude the most recent
        # UTC day as well as today. Never use future scores or data after kickoff.
        cutoff = min(now.date(), instant(s.kickoff).date()) - timedelta(days=1)
        recent_floor = now.date() - timedelta(days=90)
        url, observed = fixture.retrieval_metadata['source_url'], fixture.searched_at
        facts = []
        for team in (s.home, s.away):
            recent = sorted((r for r in rows if r['score'] is not None
                and recent_floor <= date.fromisoformat(r['date']) < cutoff
                and normalized(team) in (normalized(r['home']), normalized(r['away']))),
                key=lambda r: r['date'], reverse=True)[:5]
            for r in recent:
                home_team = normalized(team) == normalized(r['home'])
                scored, conceded = r['score'] if home_team else r['score'][::-1]
                if s.market_family == 'total':
                    category, value = 'totals', scored + conceded
                    context = f'total {value}; submitted line {s.line}'
                elif s.market_family == 'btts':
                    category, value = 'both_score', int(scored > 0 and conceded > 0)
                    context = f'both scored: {bool(value)}'
                elif s.market_family == 'handicap':
                    category, value = 'margin', scored - conceded
                    context = f'{team} goal margin {value}; submitted signed line {s.line}'
                else:
                    category, value = 'form', 'WIN' if scored > conceded else 'LOSS' if scored < conceded else 'DRAW'
                    context = f'{team} {value}, {"home" if home_team else "away"}'
                # Team names are source DATA, never instructions to the analyzer.
                claim = f'{r["date"]}: {r["home"]} {r["score"][0]}-{r["score"][1]} {r["away"]}; {context}'
                freshness = 'RECENT' if (now.date() - date.fromisoformat(r['date'])).days <= 30 else 'HISTORICAL'
                facts.append(ResearchFact(category, claim, value, 'OpenFootball EPL (CC0)', url, observed,
                    'C', r['date'], freshness, self.source_id))
        # Do not count the same H2H record twice for both participants.
        facts = tuple({(f.category, f.claim): f for f in facts}.values())
        if not facts:
            raise AuditUnavailable('insufficient_public_history')
        coords = s.coordinates()
        limits = fixture.limitations + (('Some evidence is historical, not recent form',)
                                       if any(f.freshness == 'HISTORICAL' for f in facts) else ())
        return replace(fixture, selection={k: coords[k] for k in SELECTION_FIELDS}, facts=facts,
                       limitations=limits, retrieval_metadata={k: v for k, v in fixture.retrieval_metadata.items() if k != 'rows'})


class FreeResearchResolver:
    """Local-first AuditService injection; unsupported/failure -> explicit insufficiency.

    One reviewed source per fixture in V1. No fallback to another transport,
    provider or LLM. Ambiguous adapter routing fails closed.
    """
    def __init__(self, adapters=(), cache=None):
        self.adapters = tuple(adapters)
        if len({a.source_id for a in self.adapters}) != len(self.adapters):
            raise ValueError('duplicate source adapter')
        self.cache = cache or ResearchCache()
        self.lock = threading.Lock()
        self.cache_hits = 0

    def count_cache_hit(self, now):
        with self.lock:
            self.cache_hits += 1

    def _adapter(self, s):
        matches = [a for a in self.adapters if a.supports(s)]
        if len(matches) != 1:
            raise AuditUnavailable('conflicting_source_routes' if matches else 'unsupported_free_research')
        return matches[0]

    def cached(self, s, now):
        if not s.researchable() or not instant(now) or instant(s.kickoff) <= instant(now):
            return None
        try:
            adapter = self._adapter(s)
            key = research_key(s, adapter.source_id)
            hit = self.cache.peek(key, now)
            if hit:
                return hit
            raw = self.cache.peek(self._fixture_key(s, adapter), now)
            if raw:
                # Different market on an already retrieved fixture is a cache
                # hit and consumes no extra per-ticket external-research slot.
                return self.cache.get_or_load(key, now, lambda: self._market(adapter, raw, s, now))
            return None
        except AuditUnavailable:
            return None

    def offline(self, s, now):
        """True when research for ``s`` needs no HTTP (its dataset is already cached), so a
        large ticket does not spend an uncached-research slot on it. Never fetches."""
        if not s.researchable() or not instant(now) or instant(s.kickoff) <= instant(now):
            return False
        try:
            adapter = self._adapter(s)
            check = getattr(adapter, 'offline', None)
            return bool(check and check(s, now, self.cache))
        except AuditUnavailable:
            return False

    @staticmethod
    def _fixture_key(s, adapter):
        fixture = {k: s.coordinates()[k] for k in FIXTURE_FIELDS}
        fixture['kickoff'] = instant(s.kickoff).isoformat()
        for k in ('sport', 'competition', 'home', 'away'):
            fixture[k] = normalized(fixture[k])
        return 'fixture:' + hashlib.sha256(json.dumps([fixture, adapter.source_id], sort_keys=True).encode()).hexdigest()

    @staticmethod
    def _market(adapter, raw, s, now):
        evidence = adapter.market_evidence(raw, s, now)
        return replace(evidence, fixture={k: s.coordinates()[k] for k in FIXTURE_FIELDS},
                       research_confidence=research_level(evidence, now))

    def research(self, s, now):
        now = instant(now)
        if not now or not s.researchable() or instant(s.kickoff) <= now:
            raise AuditUnavailable('incomplete_or_started_fixture')
        adapter = self._adapter(s)
        def load():
            key = self._fixture_key(s, adapter)
            raw = self.cache.get_or_load(key, now, lambda: adapter.fetch_fixture(s, now, self.cache))
            # Preserve request coordinates per leg even when raw retrieval was
            # shared across markets or equivalent UTC kickoff representations.
            return self._market(adapter, raw, s, now)
        return self.cache.get_or_load(research_key(s, adapter.source_id), now, load)

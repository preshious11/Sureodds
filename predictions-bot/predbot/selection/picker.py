"""Turn scored market lines into the day's Top Picks and Daily Picks.

Sizing: the daily post is ONE ticket, so combined odds - not pick counts -
decide how long the lists are.

Rules (from the brief):
  * top picks are the most solid selections: high probability *and* positive
    expected value with at least two agreeing sources, added best-first until
    the ticket is worth TOP_COMBINED_ODDS (~2.00)
  * daily picks then extend the same ticket until the whole list is worth
    DAILY_COMBINED_MIN_ODDS..DAILY_COMBINED_MAX_ODDS (10-20), and only with
    selections that clear the quality bar - if only 11 qualify, 11 are posted
    (never padded up to DAILY_PICKS_TARGET)
  * an outcome that is a top pick is never repeated in the daily list; the same
    match may appear with a *different* outcome, but never a contradictory one
  * the bot must not lean on one outcome type: caps on home/away/over/under
    shares, favourites, one market type, player props, exotics
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from collections import Counter
from itertools import combinations
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from predbot.config import Config
from predbot.markets.catalog import contradicts, is_equivalent, selection_family, spec
from predbot.model.ensemble import Scored
from predbot.util.names import same_team

log = logging.getLogger(__name__)


@dataclass
class PickSet:
    top: List[Scored] = field(default_factory=list)
    daily: List[Scored] = field(default_factory=list)
    pool_size: int = 0
    eligible: int = 0
    diagnostics: Dict = field(default_factory=dict)
    complete: bool = True

    def all(self) -> List[Scored]:
        return self.top + self.daily


def _settleable(s: Scored) -> bool:
    ml = s.line
    if spec(ml.market).score_settled:
        return True
    # provider-settled props: only SportsGameOdds gives us the settled value
    return ml.market == "prop" and "sportsgameodds" in s.event.sources and bool(ml.provider_odd_id)


#: Signals that are genuinely INDEPENDENT of the market. score_model is fitted
#: to the market's own main lines (Poisson/Gaussian derived from devigged
#: prices), so its "EV" is just the bookmaker margin - treating it as an
#: independent signal rejected every basketball candidate at once. Only these
#: two can say the market is wrong.
INDEPENDENT_SIGNALS = {"apif", "elo"}


def has_independent_signal(s: "Scored") -> bool:
    return bool(INDEPENDENT_SIGNALS & set(s.sources))


def _ev_tiebreak(ev: float) -> float:
    """EV mapped to [0, 1] over [-0.05, +0.10] - only ever a small tiebreaker,
    since this is a confidence product, not an edge product."""
    return min(max(ev, -0.05), 0.10) + 0.05


def _top_score(s: Scored) -> float:
    """Confidence-first: p_model dominates; EV is only a tiebreaker."""
    return 0.80 * s.p_model + 0.20 * s.agreement + 0.02 * _ev_tiebreak(s.ev)


def _daily_score(s: Scored) -> float:
    base = 0.60 * s.p_model + 0.25 * s.agreement + 0.10 * s.liquidity
    base += 0.05 * (_ev_tiebreak(s.ev) / 0.15)
    if s.line.market == "prop":
        base -= 0.05
    return base


def _cap(frac: float, target: int) -> int:
    return max(1, int(math.ceil(frac * target)))


def _same_fixture(a, b) -> bool:
    """True when two Scored candidates' events are the same real-world match,
    even if they came from different providers as two distinct Event objects
    (event.dedupe_key()/provider ids can differ across providers, so identity
    and dedupe_key alone are not enough to catch a Top/Daily duplicate)."""
    if a.sport != b.sport:
        return False
    if abs((a.start - b.start).total_seconds()) > 3 * 3600:
        return False
    return same_team(a.home, b.home) and same_team(a.away, b.away)


def _duplicates_a_top_pick(s: Scored, top: List[Scored]) -> bool:
    """True when ``s`` is the same fixture AND the same normalized outcome
    (label) as an already-chosen Top pick - blocked from Daily regardless of
    which provider each candidate came from. Same fixture with a genuinely
    different outcome is left to the existing conflicts()/contradicts() and
    per-event caps, exactly as before."""
    return any(s.label == t.label and _same_fixture(s.event, t.event) for t in top)


def choose_top(pool: List[Scored], cfg: Config):
    """Bounded deterministic search, retaining quality bars and unique matches.

    Collapse identical match/price options to the strongest, then reserve half
    the 60-option budget for strength and half for low-odds combination options.
    """
    ranked = sorted(pool, key=lambda s: (-_top_score(s), s.event.dedupe_key(), s.line.key().__repr__()))
    unique = {}
    for s in ranked:
        unique.setdefault((s.event.dedupe_key(), round(s.price, 4)), s)
    options = list(unique.values())
    if len(options) > 60:
        options = options[:30] + sorted(options[30:], key=lambda s: (s.price, -_top_score(s), s.event.dedupe_key()))[:30]
    target = cfg.top_odds_target
    best, best_key, checked = [], None, 0
    # Top Picks is a 2-3 leg accumulator: legs below top_legs_min are never
    # tried, so a single strong-priced candidate can no longer win outright.
    # Ticket sizes above 3 explode combinatorially, so the search pool is
    # trimmed further for those sizes.
    min_size = max(1, cfg.top_legs_min)
    max_size = max(min_size, cfg.top_legs_max)
    for size in range(min_size, max_size + 1):
        pool = options if size <= 3 else options[:20]
        for group in combinations(pool, size):
            if len({s.event.dedupe_key() for s in group}) != size:
                continue
            if any(_same_fixture(a.event, b.event) for a, b in combinations(group, 2)):
                # Same real fixture via two providers (fuzzy team/time match)
                # is still one bet twice - never two legs of one ticket.
                continue
            checked += 1
            price = math.prod(s.price for s in group)
            if price > cfg.top_odds_max:
                # Hard reject: never just deprioritized. A combo over the
                # ceiling is not a worse option, it is not an option.
                continue
            if price < cfg.top_odds_target:
                # The ticket must reach the 2.00 floor - shorter combos that
                # fall under it are simply not the product.
                continue
            key = (-math.prod(s.p_model for s in group),      # confidence first
                   abs(price - target),                       # then closeness to 2.0
                   -len({selection_family(s.line) for s in group}), size)
            if best_key is None or key < best_key:
                best, best_key = list(group), key
    return best, checked


def select(scored: List[Scored], cfg: Config, now: datetime,
           top_priority_source: Optional[str] = None, trace: Optional[Dict] = None) -> PickSet:
    """``trace`` (optional, diagnostics only) collects the first rule that
    blocked each candidate: id(Scored) -> reason string."""
    def _note(s: Scored, rule: str) -> None:
        """Record the FIRST rule that stopped a candidate. daily:/cap:/loop:
        reasons override earlier base:/top: ones: a candidate can fail the Top
        bar and still be a Daily candidate, so the later rule is the one that
        actually decided its fate."""
        if trace is None:
            return
        if rule.startswith(("daily:", "cap:", "loop")):
            trace[id(s)] = rule
        else:
            trace.setdefault(id(s), rule)

    ps = PickSet(pool_size=len(scored))
    lead = now + timedelta(minutes=cfg.min_lead_minutes)
    base: List[Scored] = []
    rejection_counts: Counter = Counter()
    for s in scored:
        if not s.event.start:
            rejection_counts["INVALID_KICKOFF"] += 1
            _note(s, "base: INVALID_KICKOFF")
            continue
        if s.event.start < lead:
            rejection_counts["WRONG_DATE"] += 1
            _note(s, "base: WRONG_DATE (before now+MIN_LEAD)")
            continue
        if not _settleable(s):
            rejection_counts["UNSUPPORTED_MARKET"] += 1
            _note(s, "base: UNSUPPORTED_MARKET")
            continue
        if not math.isfinite(s.price):
            rejection_counts["INVALID_PRICE"] += 1
            _note(s, "base: INVALID_PRICE (non-finite)")
            continue
        if s.price < 1.15:
            rejection_counts["INVALID_PRICE"] += 1
            _note(s, "base: INVALID_PRICE (<1.15 sanity floor)")
            continue
        base.append(s)
    ps.eligible = len(base)
    for s in base:
        s.rank_score = _daily_score(s)

    # ------------------------------------------------------------ top picks
    # Sized by odds: take the best candidates until the ticket is worth ~2.00.
    top_pool: List[Scored] = []
    top_rejected: Counter = Counter()
    for s in base:
        if s.line.market == "prop":
            top_rejected["PLAYER_PROP"] += 1
            _note(s, "top: PLAYER_PROP")
            continue
        if not (s.price >= cfg.top_leg_odds_min):     # data-sanity floor only
            top_rejected["BELOW_THRESHOLD"] += 1
            _note(s, "top: price < TOP_LEG_ODDS_MIN")
            continue
        if not (s.p_model >= cfg.top_min_prob):
            top_rejected["BELOW_THRESHOLD"] += 1
            _note(s, f"top: p_model < TOP_MIN_PROB ({cfg.top_min_prob})")
            continue
        # Confidence product, not an edge product: market-only candidates skip
        # the EV gate (their EV is negative by construction); candidates with
        # an independent signal are rejected only when that signal says the
        # market is materially overpricing the selection.
        if has_independent_signal(s) and s.ev < cfg.top_max_neg_ev:
            top_rejected["BELOW_THRESHOLD"] += 1
            _note(s, f"top: disagreement floor (ev < {cfg.top_max_neg_ev})")
            continue
        if not (s.agreement >= cfg.top_min_agreement):
            top_rejected["BELOW_THRESHOLD"] += 1
            _note(s, f"top: agreement < TOP_MIN_AGREEMENT ({cfg.top_min_agreement})")
            continue
        if not (len(s.sources) >= 2):
            top_rejected["INSUFFICIENT_SOURCES"] += 1
            _note(s, "top: INSUFFICIENT_SOURCES (<2)")
            continue
        top_pool.append(s)
    # Odds API is tried first for the Top ticket: only fall back to the full
    # pool (the other providers) when it can't reach the combined odds target.
    used_priority_pool = False
    if top_priority_source:
        priority_pool = [s for s in top_pool if top_priority_source in s.event.sources]
        top, checked = choose_top(priority_pool, cfg)
        reached = math.prod(s.price for s in top) if top else 0.0
        if top and reached >= cfg.top_odds_target:
            used_priority_pool = True
        else:
            top, checked = choose_top(top_pool, cfg)
    else:
        top, checked = choose_top(top_pool, cfg)
    top_ids = {id(s) for s in top}
    top_odds = math.prod(s.price for s in top)
    for s in top:
        s.tier = "top"
        if trace is not None:
            # A pick that makes the slate was not, in the end, blocked.
            trace.pop(id(s), None)
    ps.top = top
    ps.diagnostics = {"base_by_sport": dict(Counter(s.event.sport for s in base)),
                      "unique_candidates_by_sport": dict(Counter(sport for sport, _, _ in
                          {(s.event.sport, s.event.dedupe_key(), s.line.key()) for s in base})),
                      "gte50_by_sport": dict(Counter(s.event.sport for s in base if s.p_model >= .5)),
                      "top_eligible_by_sport": dict(Counter(s.event.sport for s in top_pool)),
                      "selection_rejections": dict(rejection_counts),
                      "top_pool_rejections": dict(top_rejected),
                      "combinations_checked": checked, "top_odds": top_odds if top else None,
                      "top_priority_source": top_priority_source,
                      "top_priority_used": used_priority_pool}

    # ---------------------------------------------------------- daily picks
    # One ticket for the whole post: keep filling while it is short of the floor,
    # stop before a pick would break the ceiling or the pick cap.
    cap = cfg.daily_target
    daily_pool: List[Scored] = []
    daily_rejected: Counter = Counter()
    for s in base:
        if id(s) in top_ids:
            _note(s, "daily: already a Top leg")
            continue
        if _duplicates_a_top_pick(s, top):
            daily_rejected["DUPLICATE_OF_TOP"] += 1
            _note(s, "daily: DUPLICATE_OF_TOP")
            continue
        # The Daily ticket stays the three-provider ticket it has always been;
        # an Odds API event that no other provider also saw is reserved for Top.
        if top_priority_source and s.event.sources == [top_priority_source]:
            daily_rejected["RESERVED_FOR_TOP_PRIORITY_SOURCE"] += 1
            _note(s, "daily: RESERVED_FOR_TOP_PRIORITY_SOURCE")
            continue
        if not (cfg.daily_min_odds <= s.price <= cfg.daily_max_odds):
            daily_rejected["BELOW_THRESHOLD"] += 1
            _note(s, f"daily: price outside DAILY_MIN_ODDS..DAILY_MAX_ODDS ({s.price:.2f})")
            continue
        if not (s.p_model >= cfg.daily_min_prob):
            daily_rejected["BELOW_THRESHOLD"] += 1
            _note(s, f"daily: p_model < DAILY_MIN_PROB ({s.p_model:.3f})")
            continue
        if has_independent_signal(s) and s.ev < cfg.daily_max_neg_ev:
            # Confidence product: market-only candidates (EV negative by
            # construction) skip the EV gate entirely; candidates with an
            # independent signal are rejected only when that signal says the
            # market is materially overpricing the selection.
            daily_rejected["BELOW_THRESHOLD"] += 1
            _note(s, f"daily: disagreement floor (ev={s.ev:.3f})")
            continue
        if not (s.agreement >= 0.30):
            daily_rejected["BELOW_THRESHOLD"] += 1
            _note(s, f"daily: agreement < 0.30 ({s.agreement:.2f})")
            continue
        daily_pool.append(s)
    preferred = [s for s in daily_pool if s.p_model >= .5]
    if preferred:
        preferred_ids = {id(s) for s in preferred}
        for s in daily_pool:
            if id(s) not in preferred_ids:
                _note(s, "daily: preferred p_model>=0.5 filter")
        daily_pool = preferred
    ps.diagnostics["daily_eligible_by_sport"] = dict(Counter(s.event.sport for s in daily_pool))
    ps.diagnostics["daily_pool_rejections"] = dict(daily_rejected)
    daily_pool.sort(key=lambda s: s.rank_score, reverse=True)

    per_event: Dict[str, int] = {}
    for t in top:
        per_event[t.event.dedupe_key()] = per_event.get(t.event.dedupe_key(), 0) + 1
    family_count: Dict[str, int] = {}
    # Keyed by (market, stat): corners totals and goals totals are both
    # market="total", and a share cap that can't tell them apart is exactly
    # how one of them ends up filling the whole list.
    market_count: Dict[Tuple[str, str], int] = {}
    stat_count: Dict[str, int] = {}
    sport_count: Dict[str, int] = {}
    league_count: Dict[str, int] = {}
    favs = props = exotics = 0
    sports_available = {s.event.sport for s in daily_pool}
    caps = {"family": _cap(0.45, cap), "favs": _cap(0.50, cap), "exotic": _cap(0.25, cap),
            "league": cfg.daily_max_per_league}
    # The list is sized by combined odds, not by a count, so a 25% market
    # share has to be measured against the list as it is actually being
    # built - a share of daily_target (20) would still let one market take
    # 5 of a 6-pick slate.
    market_share = cfg.daily_max_market_share
    # Same measurement basis for the stat share: corners reach the list through
    # several markets (totals AND 1X2), so the (market, stat) cap alone still
    # lets "corners, corners, corners" happen.
    stat_share = cfg.daily_max_stat_share
    sport_share = cfg.daily_max_sport_share if len(sports_available) > 1 else 1.0
    relaxed_hits: Counter = Counter()

    def share_exceeded(count: int, share: float) -> bool:
        n_after = len(chosen) + 1
        return count + 1 > max(1, math.ceil(share * n_after))

    chosen: List[Scored] = []
    chosen_ids: set = set()

    def conflicts(s: Scored) -> bool:
        for other in top + chosen:
            if other.event.dedupe_key() != s.event.dedupe_key():
                continue
            if is_equivalent(other.line, s.line) or contradicts(other.line, s.line):
                return True
        return False

    def try_add(s: Scored, relaxed: bool) -> bool:
        """The strict pass enforces every quota. The relaxed pass - run only
        when the strict ticket is short of DAILY_COMBINED_MIN_ODDS - waives
        the market/sport/league shares (never the per-event, conflict,
        family, favourite, prop or exotic limits) and records which quota
        each admitted pick would otherwise have broken."""
        nonlocal favs, props, exotics
        ek = s.event.dedupe_key()
        fam = selection_family(s.line)
        mk = s.line.market
        mkey = (mk, s.line.stat)
        grp = spec(mk).group
        if per_event.get(ek, 0) >= cfg.max_picks_per_event:
            _note(s, f"cap: per-event (MAX_PICKS_PER_EVENT={cfg.max_picks_per_event})")
            return False
        if conflicts(s):
            _note(s, "cap: conflict with chosen")
            return False
        if family_count.get(fam, 0) >= caps["family"] and fam != "other":
            _note(s, f"cap: outcome family '{fam}' ({caps['family']})")
            return False
        if s.price < 1.50 and favs >= caps["favs"]:
            _note(s, f"cap: favourites ({caps['favs']})")
            return False
        if mk == "prop" and props >= cfg.max_player_props:
            _note(s, "cap: player props")
            return False
        if grp == "exotic" and exotics >= caps["exotic"]:
            _note(s, "cap: exotics")
            return False
        over = {"market": share_exceeded(market_count.get(mkey, 0), market_share),
                # "points" is the DEFAULT stat every main market carries (match
                # result, totals, handicaps); it is not a derived stat like
                # corners/cards. Capping it here would cap all main-line bets
                # combined - it silently blocked every basketball candidate
                # (basketball has no non-points markets), duplicating the
                # (market, stat) cap above. The stat cap exists to stop
                # "corners, corners, corners", so it applies to DERIVED stats.
                "stat": (bool(s.line.stat) and s.line.stat != "points"
                         and share_exceeded(stat_count.get(s.line.stat, 0), stat_share)),
                "sport": share_exceeded(sport_count.get(s.event.sport, 0), sport_share),
                "league": bool(s.event.league) and league_count.get(s.event.league, 0) >= caps["league"]}
        if any(over.values()):
            if not relaxed:
                for name, hit in over.items():
                    if hit:
                        _note(s, f"cap: share '{name}' ({name} share exceeded)")
                return False
            for name, hit in over.items():
                if hit:
                    relaxed_hits[name] += 1
        chosen.append(s)
        if trace is not None:
            # A pick that makes the slate was not, in the end, blocked.
            trace.pop(id(s), None)
        chosen_ids.add(id(s))
        per_event[ek] = per_event.get(ek, 0) + 1
        family_count[fam] = family_count.get(fam, 0) + 1
        market_count[mkey] = market_count.get(mkey, 0) + 1
        if s.line.stat and s.line.stat != "points":
            stat_count[s.line.stat] = stat_count.get(s.line.stat, 0) + 1
        sport_count[s.event.sport] = sport_count.get(s.event.sport, 0) + 1
        league_count[s.event.league] = league_count.get(s.event.league, 0) + 1
        favs += 1 if s.price < 1.50 else 0
        props += 1 if mk == "prop" else 0
        exotics += 1 if grp == "exotic" else 0
        return True

    ticket = top_odds

    def under_cap() -> bool:
        return len(top) + len(chosen) < cap        # "up to DAILY_PICKS_TARGET picks" in one list

    def fits(price: float) -> bool:
        """Never accept a pick that would push the ticket past the ceiling, floor or not."""
        if ticket >= cfg.daily_odds_max:
            return False
        return ticket * price <= cfg.daily_odds_max

    for i, s in enumerate(daily_pool):
        if not under_cap():
            for rest in daily_pool[i:]:
                _note(rest, "loop: DAILY_PICKS_TARGET reached (never attempted)")
            break
        if not fits(s.price):
            _note(s, f"loop: ticket x {s.price:.2f} would exceed DAILY_COMBINED_MAX_ODDS")
            continue
        if try_add(s, relaxed=False):
            ticket *= s.price
    strict_ticket, strict_picks = ticket, len(chosen)
    if ticket < cfg.daily_odds_min:
        for s in daily_pool:
            if not under_cap():
                break
            if id(s) in chosen_ids:
                _note(s, "loop: already chosen")
                continue
            if not fits(s.price):
                _note(s, f"loop(relaxed): ticket x {s.price:.2f} would exceed DAILY_COMBINED_MAX_ODDS")
                continue
            if try_add(s, relaxed=True):
                ticket *= s.price
        if relaxed_hits:
            ps.diagnostics["daily_caps_relaxed"] = {
                "reason": "strict pass could not reach DAILY_COMBINED_MIN_ODDS",
                "strict_ticket": round(strict_ticket, 2), "strict_picks": strict_picks,
                "daily_pool": len(daily_pool), "exceeded": dict(relaxed_hits)}
            log.warning("picker: diversity quotas relaxed %s - strict ticket %.2f from %d picks was "
                        "below the %.2f floor with a qualifying pool of %d",
                        dict(relaxed_hits), strict_ticket, strict_picks, cfg.daily_odds_min, len(daily_pool))

    for s in chosen:
        s.tier = "daily"
    ps.daily = chosen
    log.info("picker: pool=%d eligible=%d top_pool=%d daily_pool=%d -> top=%d daily=%d ticket=%.2f",
             ps.pool_size, ps.eligible, len(top_pool), len(daily_pool), len(top), len(chosen), ticket)
    ps.diagnostics["daily_ticket_by_sport"] = dict(Counter(s.event.sport for s in ps.all()))
    ps.diagnostics["daily_by_market"] = dict(Counter(f"{s.line.market}/{s.line.stat}" for s in ps.daily))
    ps.diagnostics["daily_by_league"] = dict(Counter(s.event.league for s in ps.daily))
    ps.diagnostics["top_selected_by_sport"] = dict(Counter(s.event.sport for s in ps.top))
    log.info("selection diagnostics: %s", ps.diagnostics)
    return ps

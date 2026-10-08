"""Private audit presentation: escaped, bounded blocks; no generated user-facing prose."""
from predbot.bot.telegram import esc, _split
from predbot.ticket_audit.composition import finish, local_probability

LABELS = {'STRONG': '🟢 Strong', 'REASONABLE': '🟡 Reasonable', 'RISKY': '🟠 Risky',
          'VERY_RISKY': '🔴 Very Risky', 'INSUFFICIENT_EVIDENCE': '⚪ Insufficient Evidence'}


def odds(value):
    """Bookmaker-supplied odds as given (10 significant digits; :g would round 12345.67)."""
    return f'{value:.10g}'


def field(value, limit=180):
    text = str(value)
    return esc(text[:limit] + ('…' if len(text) > limit else ''))


def fixture(s):
    return ' vs '.join(field(v) for v in (s.home, s.away) if v)


def pick(s):
    side = {'home': s.home, 'away': s.away, 'draw': 'Draw'}.get(s.selection, s.selection)
    if s.market_family in ('result3', 'result2'):
        return field(side or 'Selection') + (' to Win' if s.selection in ('home', 'away') else '')
    if s.market_family in ('total', 'handicap'):
        line = f'{s.line:+g}' if s.line is not None and s.market_family == 'handicap' else f'{s.line:g}' if s.line is not None else ''
        return ' '.join(v for v in (field(side) if side else '', line,
            'Handicap' if s.market_family == 'handicap' else '') if v)
    if s.market_family == 'btts':
        return 'BTTS' + (' ' + field(s.selection.title()) if s.selection else '')
    if s.market_family in ('dc', 'dnb'):
        return ('Double Chance' if s.market_family == 'dc' else 'Draw No Bet') + (' ' + field(side) if side else '')
    return field(s.selection or s.raw_text or s.market_family or 'Selection')


def review(ticket):
    n = len(ticket.selections)
    blocks = [f'🎟 I found {n} selection{"s" if n != 1 else ""}'] + _legs(ticket)
    if ticket.submitted_combined_odds is not None:
        blocks.append(f'Combined odds: {odds(ticket.submitted_combined_odds)}')
    blocks.append('Does this look right?')
    return pack_blocks(blocks)


def pack_blocks(blocks, limit=3500):
    """Pack complete selection blocks dynamically; split only an oversized block."""
    pages, current = [], ''
    for block in blocks:
        if not block:
            continue
        proposed = current + '\n\n' + block if current else block
        if len(proposed) <= limit:
            current = proposed
            continue
        if current:
            pages.append(current)
            current = ''
        if len(block) > limit:
            pages.extend(_split(block, limit=limit))
        else:
            current = block
    if current:
        pages.append(current)
    return pages


MARKET_NAMES = {'result3': '1X2', 'result2': 'Winner', 'total': 'Over/Under', 'handicap': 'Handicap',
                'btts': 'Both Teams To Score', 'dc': 'Double Chance', 'dnb': 'Draw No Bet'}
PERIOD_PREFIX = {'1h': '1st Half', '2h': '2nd Half'}


def market_name(s):
    """Natural market wording; canonical ids (result2/result3/...) are never shown."""
    name = MARKET_NAMES.get(s.market_family)
    if not name:
        return None
    prefix = PERIOD_PREFIX.get(s.period)
    return f'{prefix} {name}' if prefix else name


def booking_review(ticket, result):
    """Same review, plus code provenance and explicit unresolved legs. Nothing substituted."""
    n = len(ticket.selections)
    header = (f'🎟 {field(ticket.source_bookmaker or "Booking")} code {field(result.booking_code, 24)} — '
              f'{n} selection{"s" if n != 1 else ""} loaded')
    if result.status != 'OK':
        header += ('\n\n⚠️ I could only partly decode this code. Selections marked ⚠️ couldn’t be read fully '
                   'and will be marked Insufficient Evidence.')
    blocks = [header]
    for line in result.unresolved:
        blocks.append('⚠️ ' + field(line, 200))
    for block, leg, s in zip(_legs(ticket), result.selections, ticket.selections):
        name = market_name(s)
        if name:
            block += f'\nMarket: {name}'
        if leg.unresolved:
            missing = ', '.join(field(k.replace('_', ' ')) for k in leg.unresolved)
            block += f'\n⚠️ Not decoded: {missing}'
        blocks.append(block)
    if ticket.submitted_combined_odds is not None:
        blocks.append(f'Combined odds: {odds(ticket.submitted_combined_odds)}')
    blocks.append('Is this the right ticket?')
    return pack_blocks(blocks)


def _legs(ticket):
    blocks = []
    for i, s in enumerate(ticket.selections, 1):
        title = fixture(s)
        description = pick(s)
        if s.submitted_odds is not None:
            description += f' @ {odds(s.submitted_odds)}'
        lines = [f'{i}. <b>{title or description}</b>']
        if title: lines.append(description)
        context = [field(v) for v in (s.sport, s.competition, s.kickoff) if v]
        if context: lines.append(' · '.join(context))
        if any((s.period, s.stat, s.entity)):
            period = {'full':'Full match','reg':'Regulation','1h':'First half','2h':'Second half'}.get(s.period,s.period)
            stat = {'points':'Score'}.get(s.stat,s.stat)
            entity = {'all':'Both teams','home':s.home or 'Home','away':s.away or 'Away'}.get(s.entity,s.entity)
            lines.append(' · '.join(field(v) for v in (period,stat,entity) if v))
        blocks.append('\n'.join(lines))
    return blocks


def results(ticket, audit):
    audit = finish(audit)
    count = sum(a.verdict != 'INSUFFICIENT_EVIDENCE' for a in audit.selections)
    summary = f'🎟 Ticket Audit\n\n{len(audit.selections)} selections'
    if ticket.submitted_combined_odds is not None:
        summary += f' · Combined odds {odds(ticket.submitted_combined_odds)}'
    summary += f'\nEvidence: {count}/{len(audit.selections)} selections checked'
    pages = [summary]
    blocks = []
    for i, a in enumerate(audit.selections, 1):
        s = a.normalized_selection
        label = LABELS[a.verdict]
        title = fixture(s)
        p = local_probability(a)
        source = ('Today’s data' if a.evidence.evidence_source == 'LOCAL' and a.evidence_level in ('GOOD_LOCAL','STRONG_LOCAL')
                  else 'Public data' if a.evidence_level == 'RESEARCHED' else 'Limited data')
        description = ('I don’t have enough reliable data to judge this selection without guessing.' if a.verdict == 'INSUFFICIENT_EVIDENCE'
            else 'The available evidence indicates substantial risk.' if a.verdict == 'VERY_RISKY'
            else 'The available evidence indicates risk.' if a.verdict == 'RISKY'
            else 'Supported by today’s evaluation.' if source == 'Today’s data'
            else 'Supported by the available public evidence; coverage is limited.')
        lines = [f'{i}. <b>{pick(s)}</b>', title, label, source]
        if p is not None: lines.append(f'Model probability: {p:.0%}')
        lines.append(description)
        # V1 foundation supplies no independently verified safer alternative.
        # Never render an injected/free-text alternative or technical concerns.
        blocks.append('\n'.join(line for line in lines if line))
    pages.extend(pack_blocks(blocks))
    footer = []
    if audit.weakest_selections:
        a = audit.selections[audit.weakest_selections[0]]
        footer.append('⚠️ Weakest assessed leg\n' + fixture(a.normalized_selection) + ' — ' + pick(a.normalized_selection))
    footer.append('Overall: ' + LABELS[audit.overall_verdict])
    if count < len(audit.selections): footer.append('Some selections have insufficient evidence. Their outcomes are unknown.')
    if len(audit.selections) >= 6: footer.append('More selections add combined uncertainty.')
    pages.append('\n\n'.join(footer))
    return pages

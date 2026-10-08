"""Team-name normalisation and fuzzy matching (used to de-duplicate the same
fixture across providers and to match results back to picks)."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache

#: Generic club-name affixes only, never a word that distinguishes one real
#: club from another (e.g. "united"/"city"/"real"/"atletico" must stay OUT of
#: this list - stripping them made "Manchester United" == "Manchester City"
#: and "Real Madrid" == "Atletico Madrid" under same_team()).
_STOP = {"fc", "cf", "sc", "afc", "club", "de", "the", "cd", "ac", "as", "us", "ss", "bk", "if", "sk",
         "fk", "cs", "rc", "sv", "vfb", "vfl", "tsg", "1", "sporting", "rcd", "ud", "sd"}

#: Sports whose US-style names come as "BOS Red Sox" / "LA Dodgers" / "Athletics" on one provider
#: and "Boston Red Sox" / "Los Angeles Dodgers" / "Oakland Athletics" on another.
NICKNAME_SPORTS = {"baseball", "basketball", "american_football"}

#: A reserve/youth-team qualifier is never noise to strip: "Barcelona" and
#: "Barcelona B" are different clubs, not the same club spelled differently.
_RESERVE_SUFFIXES = {"b", "ii", "iii", "reserves", "reserve", "u23", "u21", "u20", "u19", "u18", "youth"}


@lru_cache(maxsize=65536)
def normalize(name: str) -> str:
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t not in _STOP]
    return " ".join(tokens) if tokens else s.strip()


@lru_cache(maxsize=262144)
def similarity(a: str, b: str, floor: float = 0.0) -> float:
    """Name similarity in [0, 1]. ``floor`` is an optimisation only: when the cheap upper bounds of the ratio
    already prove it is below ``floor``, 0.0 is returned without running the full matcher - so a caller that
    only asks "is it >= floor?" (same_team) gets the identical answer."""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    extra = (ta - tb) | (tb - ta)
    if extra and extra <= _RESERVE_SUFFIXES:
        return 0.0
    if ta and tb and len(ta) > 1 and len(tb) > 1 and (ta <= tb or tb <= ta):
        return 0.92
    sm = SequenceMatcher(None, na, nb)
    if floor and (sm.real_quick_ratio() < floor or sm.quick_ratio() < floor):
        return 0.0
    return sm.ratio()


def is_doubles(home: str, away: str) -> bool:
    """A tennis doubles fixture: a side is a pair ("Dabrowski / Stefani"). Neither SharpAPI's ``/odds`` rows nor
    the Odds API carry a doubles flag or participant count, so the pair separator is the indicator."""
    return " / " in (home or "") or " / " in (away or "")


def strip_abbreviation(name: str) -> str:
    """Drop a leading/trailing all-caps 2-3 letter city code ("BOS Red Sox" -> "Red Sox", "LA Dodgers" ->
    "Dodgers"); a name that is nothing but the code is left alone."""
    tokens = (name or "").split()
    if len(tokens) >= 2 and tokens[0].isalpha() and tokens[0].isupper() and len(tokens[0]) <= 3:
        tokens = tokens[1:]
    if len(tokens) >= 2 and tokens[-1].isalpha() and tokens[-1].isupper() and len(tokens[-1]) <= 3:
        tokens = tokens[:-1]
    return " ".join(tokens)


#: Last words that name an institution or a kind of place, not a team: "Ohio State" and "Kent State" do not share a
#: nickname. A name ending in one of these (or in a single letter, "Texas A&M") has no nickname to reduce to.
GENERIC_TAILS = {"state", "university", "college", "tech", "institute", "polytechnic", "city", "county"}


#: The professional franchises whose nickname is UNIQUE within its league, with the place names a provider puts in
#: front of it. Inside that league "LA Dodgers", "Los Angeles Dodgers" and "Dodgers" are one team. A name is reduced
#: only when it is the nickname alone or one of THAT franchise's places + the nickname, so "Penn State Nittany Lions"
#: or "Louisville Cardinals" never become a professional franchise. A last word is never an identity on its own:
#: college teams share mascots ("Kentucky Wildcats" / "Arizona Wildcats") and European clubs share cities
#: ("Maccabi Tel Aviv" / "Hapoel Tel Aviv"); reducing those to one word merged different teams into one.
_LA = ("los angeles", "la")
PRO_FRANCHISES = {
    "nba": {"hawks": ("atlanta",), "celtics": ("boston",), "nets": ("brooklyn", "new jersey"), "hornets": ("charlotte",),
            "bulls": ("chicago",), "cavaliers": ("cleveland",), "mavericks": ("dallas",), "nuggets": ("denver",),
            "pistons": ("detroit",), "warriors": ("golden state",), "rockets": ("houston",), "pacers": ("indiana",),
            "clippers": _LA, "lakers": _LA, "grizzlies": ("memphis",), "heat": ("miami",), "bucks": ("milwaukee",),
            "timberwolves": ("minnesota",), "pelicans": ("new orleans",), "knicks": ("new york",),
            "thunder": ("oklahoma city", "oklahoma"), "magic": ("orlando",), "76ers": ("philadelphia",),
            "suns": ("phoenix",), "trail blazers": ("portland",), "blazers": ("portland",), "kings": ("sacramento",),
            "spurs": ("san antonio",), "raptors": ("toronto",), "jazz": ("utah",), "wizards": ("washington",)},
    "mlb": {"diamondbacks": ("arizona",), "braves": ("atlanta",), "orioles": ("baltimore",), "red sox": ("boston",),
            "cubs": ("chicago",), "white sox": ("chicago",), "reds": ("cincinnati",), "guardians": ("cleveland",),
            "rockies": ("colorado",), "tigers": ("detroit",), "astros": ("houston",), "royals": ("kansas city",),
            "angels": _LA + ("anaheim", "los angeles anaheim"), "dodgers": _LA, "marlins": ("miami", "florida"),
            "brewers": ("milwaukee",), "twins": ("minnesota",), "mets": ("new york",), "yankees": ("new york",),
            "athletics": ("oakland", "sacramento", "las vegas"), "phillies": ("philadelphia",),
            "pirates": ("pittsburgh",), "padres": ("san diego",), "giants": ("san francisco",),
            "mariners": ("seattle",), "cardinals": ("st louis", "saint louis"), "rays": ("tampa bay", "tampa"),
            "rangers": ("texas",), "blue jays": ("toronto",), "nationals": ("washington",)},
    "nfl": {"cardinals": ("arizona",), "falcons": ("atlanta",), "ravens": ("baltimore",), "bills": ("buffalo",),
            "panthers": ("carolina",), "bears": ("chicago",), "bengals": ("cincinnati",), "browns": ("cleveland",),
            "cowboys": ("dallas",), "broncos": ("denver",), "lions": ("detroit",), "packers": ("green bay",),
            "texans": ("houston",), "colts": ("indianapolis",), "jaguars": ("jacksonville",), "chiefs": ("kansas city",),
            "raiders": ("las vegas", "oakland"), "chargers": _LA, "rams": _LA, "dolphins": ("miami",),
            "vikings": ("minnesota",), "patriots": ("new england",), "saints": ("new orleans",), "giants": ("new york",),
            "jets": ("new york",), "eagles": ("philadelphia",), "steelers": ("pittsburgh",), "49ers": ("san francisco",),
            "seahawks": ("seattle",), "buccaneers": ("tampa bay",), "titans": ("tennessee",),
            "commanders": ("washington",)},
}
PRO_NICKNAMES = {league: set(teams) for league, teams in PRO_FRANCHISES.items()}
PRO_LEAGUE_OF_SPORT = {"basketball": "nba", "baseball": "mlb", "american_football": "nfl"}

#: Tokens that never distinguish one club from another within a sport's naming ("Valencia Basket" is "Valencia"
#: in basketball, "Bayer 04 Leverkusen" is "Bayer Leverkusen"). Digits are treated the same way.
_AFFIX_NOISE = {"calcio", "football", "futbol", "fussball", "basket", "basketball", "baloncesto", "basquet",
                "bc", "kk", "bk", "hc", "bb", "pallacanestro", "team"}

#: Tokens that qualify a SCHOOL rather than name a mascot: "Michigan" and "Michigan State", "Texas" and "Texas A&M",
#: "Kentucky" and "Western Kentucky" are different schools.
_SCHOOL_QUALIFIERS = GENERIC_TAILS | {"north", "south", "east", "west", "northern", "southern", "eastern",
                                     "western", "central", "upstate", "international", "christian", "saint", "st"}


def _pro_league(sport: str, competition: str):
    """The professional league whose curated nicknames apply: the event's competition when known (``None`` for
    any other competition), else the sport's single professional league."""
    if competition:
        return competition if competition in PRO_NICKNAMES and PRO_LEAGUE_OF_SPORT.get(sport) == competition else None
    return PRO_LEAGUE_OF_SPORT.get(sport)


def _franchise(name: str, league: str) -> str:
    """The franchise key when ``name`` is a curated franchise of ``league`` - the nickname alone, or one of that
    franchise's places (a leading city code is dropped first: "LA", "BOS") followed by it - else "". The key keeps
    the form earlier releases stored (participant ids and fixture keys derive from it): the last word, except
    "red sox" / "white sox" which need both."""
    tokens = normalize(strip_abbreviation(name)).split()
    franchises = PRO_FRANCHISES[league]
    for width in (2, 1):
        nickname = " ".join(tokens[-width:]) if len(tokens) >= width else ""
        if nickname in franchises:
            place = " ".join(tokens[:-width])
            if place and place not in franchises[nickname]:
                return ""
            return " ".join(tokens[-2:]) if tokens[-1] == "sox" else tokens[-1]
    return ""


def canonical_team(name: str, sport: str = "", competition: str = "") -> str:
    """A spelling-independent identity token for a team, used for fixture keys and participant identity.

    The normalised full name everywhere, except a curated professional franchise nickname (NBA/MLB/NFL, unique
    within the league): "BOS Red Sox" and "Boston Red Sox" are "red sox". ``competition`` (a canonical competition
    id) scopes that: inside NCAA or EuroLeague nothing is reduced, so "LSU Tigers" never becomes MLB's "tigers".
    Without a competition the sport's professional league is assumed."""
    if sport in NICKNAME_SPORTS:
        league = _pro_league(sport, competition)
        if league:
            nickname = _franchise(name, league)
            if nickname:
                return nickname
    return normalize(name)


def _osa(a: str, b: str) -> int:
    """Optimal-string-alignment edit distance (adjacent transpositions count once)."""
    d = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a) + 1):
        d[i][0] = i
    for j in range(len(b) + 1):
        d[0][j] = j
    for i in range(1, len(a) + 1):
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            d[i][j] = min(d[i - 1][j] + 1, d[i][j - 1] + 1, d[i - 1][j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                d[i][j] = min(d[i][j], d[i - 2][j - 2] + 1)
    return d[len(a)][len(b)]


def _is_subsequence(short: str, long_: str) -> bool:
    it = iter(long_)
    return all(ch in it for ch in short)


def _token_match(x: str, y: str):
    """'exact', 'typo' (one edit in a long word: "olympiakos"/"olympiacos"), 'abbr' ("utd"/"united", "k"/"karolina")
    or None. "niger"/"nigeria" and "austria"/"australia" are two edits apart, so different words."""
    if x == y:
        return "exact"
    if len(x) >= 6 and len(y) >= 6 and _osa(x, y) <= 1:
        return "typo"
    short, long_ = sorted((x, y), key=len)
    if len(short) <= 4 and len(long_) >= len(short) + 2 and short[0] == long_[0] and _is_subsequence(short, long_):
        return "abbr"
    return None


def _aligned(ta, tb) -> bool:
    """Every word pairs with a word of the other name, at least one pair is exact, and an abbreviation is only
    trusted alongside another word (a lone "man" is not "manchester")."""
    kinds = [_token_match(x, y) for x, y in zip(ta, tb)]
    if any(k is None for k in kinds) or "exact" not in kinds and "typo" not in kinds:
        return False
    return "abbr" not in kinds or len(kinds) >= 2


def _mascot(token: str) -> bool:
    return len(token) >= 3 and not token.isdigit() and token not in _SCHOOL_QUALIFIERS and token not in _RESERVE_SUFFIXES


def same_participant(a: str, b: str, sport: str = "", competition: str = "") -> bool:
    """Strict identity test: do two spellings name the SAME participant? Used for identity and fixture merging,
    where a wrong "yes" creates a fixture that does not exist, so plausible-but-different names are NO:
    Nigeria/Niger, Austria/Australia, Real Madrid/Real Madrid Castilla, Kentucky/Western Kentucky,
    Maccabi Tel Aviv/Hapoel Tel Aviv, Karolina/Kristyna Pliskova.

    Yes for: the same canonical key (incl. a curated pro franchise), word order ("Pliskova K." / "Karolina
    Pliskova"), generic affixes and digits ("Bayer 04 Leverkusen"), one typo in a long word, abbreviations beside
    an exact word ("Sheffield Weds"), and - in US-style sports, outside the pro leagues - a school name plus its
    mascot ("Ohio State" / "Ohio State Buckeyes"), never a school qualifier ("Michigan" / "Michigan State")."""
    ka, kb = canonical_team(a, sport, competition), canonical_team(b, sport, competition)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    ta, tb = ka.split(), kb.split()
    diff = set(ta) ^ set(tb)
    if diff and diff <= _RESERVE_SUFFIXES:
        return False
    if (set(ta) & _RESERVE_SUFFIXES) != (set(tb) & _RESERVE_SUFFIXES):
        return False
    fa = [t for t in ta if t not in _AFFIX_NOISE and not t.isdigit()]
    fb = [t for t in tb if t not in _AFFIX_NOISE and not t.isdigit()]
    if not fa or not fb:
        return False
    if sorted(fa) == sorted(fb):
        return True
    if sport in NICKNAME_SPORTS and len(fa) != len(fb):
        short, long_ = (fa, fb) if len(fa) < len(fb) else (fb, fa)
        if long_[:len(short)] == short and all(_mascot(t) for t in long_[len(short):]):
            return True
    if len(fa) != len(fb):
        return False
    return _aligned(fa, fb) or _aligned(sorted(fa), sorted(fb))


def _significant(name: str):
    return {t for t in normalize(name).split() if len(t) >= 4 and t not in _AFFIX_NOISE
            and t not in _SCHOOL_QUALIFIERS and not t.isdigit()}


def related_names(a: str, b: str, sport: str = "", competition: str = "") -> bool:
    """The minimum NAME evidence before a spelling may be learned as an alias from fixture context (same
    competition, same known opponent, same kickoff): a strict match, a shared significant word ("Troyes" /
    "ESTAC Troyes"), a shared stem ("Lyon" / "Olympique Lyonnais") or close spelling. Context alone - an unknown
    name playing a known club's opponent - is never enough: "Valencia Basket" is not "Milano"."""
    if same_participant(a, b, sport, competition):
        return True
    sa, sb = _significant(a), _significant(b)
    if sa & sb:
        return True
    for x in sa:
        for y in sb:
            n = len(x) if x == y else next((i for i, (p, q) in enumerate(zip(x, y)) if p != q), min(len(x), len(y)))
            if n >= 4 and n >= 0.6 * min(len(x), len(y)):
                return True
    return similarity(a, b) >= 0.6


#: Words that make a different team of the same base name: reserve / youth / women's sides. Kept apart from
#: _RESERVE_SUFFIXES (used by participant identity) so this veto cannot change identity keys.
_DISTINCT_SIDES = _RESERVE_SUFFIXES | {"castilla", "promesas", "women", "womens", "w", "ladies", "femenino",
                                       "feminin", "femminile", "frauen", "u17", "u16"}
#: School qualifiers that make a different college ("Western Kentucky", "Michigan State", "Texas Tech"). "city" and
#: "county" are left out on purpose: "Leicester" and "Leicester City" are one club.
_DISTINCT_QUALIFIERS = (_SCHOOL_QUALIFIERS - {"city", "county", "saint", "st"}) | {"a", "m"}


def _side_tokens(name: str) -> List[str]:
    tokens = ["saint" if t == "st" else t for t in normalize(name).split()]
    return [t for t in tokens if t not in _AFFIX_NOISE and not t.isdigit()]


def distinct_participants(a: str, b: str, sport: str = "") -> bool:
    """True when two names provably belong to DIFFERENT teams even though a fuzzy matcher may find them alike:
    a reserve / women's side of the same club ("Real Madrid" / "Real Madrid Castilla"), a different school
    ("Kentucky" / "Western Kentucky", "Michigan" / "Michigan State", "Texas" / "Texas A&M"), or two different
    single words that are not a one-letter typo of each other ("Nigeria" / "Niger", "Austria" / "Australia")."""
    if same_participant(a, b, sport):
        return False
    ta, tb = _side_tokens(a), _side_tokens(b)
    if not ta or not tb:
        return False
    diff = set(ta) ^ set(tb)
    if diff & _DISTINCT_SIDES or diff & _DISTINCT_QUALIFIERS:
        return True
    if len(ta) == 1 and len(tb) == 1:
        return _token_match(ta[0], tb[0]) not in ("exact", "typo")
    return False


def same_team(a: str, b: str, threshold: float = 0.82, sport: str = "") -> bool:
    """Lenient same-club test for matching RESULT feeds to a known fixture (settlement, live orientation): the
    strict ``same_participant`` first, then fuzzy spelling. ``sport`` (optional) switches on the US-sports rules:
    city abbreviations are stripped, and when one name's words are the END of the other's ("Rangers" / "Texas
    Rangers", "Athletics" / "Oakland Athletics") they are the same team. The threshold is never lowered, and no
    fuzzy rule may match two names that ``distinct_participants`` proves different (Nigeria/Niger,
    Kentucky/Western Kentucky, Real Madrid/Castilla)."""
    if same_participant(a, b, sport):
        return True
    if distinct_participants(a, b, sport):
        return False
    if similarity(a, b, threshold) >= threshold:
        return True
    if sport in NICKNAME_SPORTS:
        sa, sb = strip_abbreviation(a), strip_abbreviation(b)
        if similarity(sa, sb, threshold) >= threshold:
            return True
        ta, tb = normalize(sa).split(), normalize(sb).split()
        if ta and tb:
            short, long_ = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
            if len(short) == 1 and (short[0] in GENERIC_TAILS or len(short[0]) == 1):
                return False
            return long_[-len(short):] == short
    return False

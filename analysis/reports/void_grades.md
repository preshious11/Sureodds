# B1 — Deadline-expired voids, re-graded in memory

Generated 2026-10-23 from production D1 (read-only SELECTs) by `analysis/pull_data.py` + `analysis/grade_voids.py`.
**Nothing was written to production.** Grades exist only in `analysis/data/void_grades.csv`.

## Method

- Population: every production pick with `status = 'void'` and `result_json.reason = "settlement deadline expired"`:
  **186 picks on 47 fixtures** (pick dates 2026-10-14 … 2026-10-19). Membership: 177 are `all`-only, 7 are also in
  Daily, 2 also in Top.
- Lookup and grading use production code unchanged: `ResultsResolver.paths_for` / `_ask`
  (`predbot/providers/chain.py`) and `predbot.markets.settlement.grade`. Only the free sources were used:
  ESPN, EuroLeague, football-data.org (key loaded from `predictions-bot/.env`, never printed). No odds-provider calls.
- Grading source = the first free source, in the resolver's own order, that returned a usable result (football-data
  comes before ESPN for the competitions it covers). Every other free source on the path was also asked, for a
  cross-check (`alt_*` columns in the CSV).

## Result

| Outcome | Count |
|---|---|
| won | 98 |
| lost | 57 |
| push | 4 |
| not obtainable | 27 |
| **total** | **186** |

By grading source:

| Source | won | lost | push | not obtainable | total |
|---|---|---|---|---|---|
| espn | 48 | 29 | 4 | – | 81 |
| football_data | 48 | 22 | – | – | 70 |
| euroleague | 2 | 6 | – | – | 8 |
| none | – | – | – | 27 | 27 |

By competition and source: MLS (espn) 33 W / 19 L / 4 P, 5 not obtainable; UCL (football_data) 34 W / 16 L (both
league spellings), UCL (espn) 3 W / 2 L; Championship (football_data) 14 W / 6 L; NBA (espn) 8 W / 7 L;
EuroLeague (euroleague) 2 W / 6 L, 20 not obtainable; NCAAF (espn) 3 W / 1 L; MLB (espn) 1 W; UEL 1 and EPL 1 not
obtainable.

**Cross-check:** 50 picks were graded by both ESPN and football-data.org. They agreed on all 50.

**Compared with the earlier ESPN/EuroLeague-only run** (84 W / 51 L / 4 P / 47 not obtainable): football-data.org
graded 20 more picks (14 W / 6 L), all of them Championship. ESPN could not grade those because of a competition-lookup
gap. These are Odds-API picks stored with league `"Championship"`, `_league_id` empty and `_sport_key =
"soccer_efl_champ"`. The resolver passes only `league_id` and `league` to `find_result`, and
`lookup("", "Championship")` returns None, so ESPN never knows which competition to search. football-data.org still
found them because, when it gets no competition code, it falls back to its all-competitions `/matches` endpoint.
`lookup("soccer_efl_champ")` would have resolved it to `championship`.

## Why the remaining 27 are not obtainable

| Fixtures | Picks | Cause (evidence in `analysis/data/unmatched_probe.json`) |
|---|---|---|
| 7 women's EuroLeague games (e.g. "Valencia Basket Club [W] v Fenerbahce SK [W]") | 20 | Stored under league "Euroleague", which resolves to the men's EuroLeague feed (code `E`). That feed lists only 3 games on 2026-10-14, all men's, so these games cannot be matched. This is a competition-identity problem, not a results outage. |
| DC United v New York Red Bulls (MLS, 2026-10-14) | 5 | ESPN has the game (D.C. United 2–1 "Red Bull New York" / "Red Bull NY", full time). The home side matches; "New York Red Bulls" vs "Red Bull New York" does not (word order plus bull/bulls). |
| Uniao Torreense v Sunderland (UEL, 2026-10-15) | 1 | See the UEL verdict below. |
| Tottenham v Coventry (EPL, 2026-10-19) | 1 | ESPN sends "Tottenham Hotspur"/"Spurs" and football-data sends "Coventry City FC"/"Coventry City", so each source fails on one side. |

If the names had matched, the scores these sources already hold would grade them (computed in memory with
`settlement.grade`, saved in `analysis/data/name_gap_hypothetical_grades.txt`; **not** included in the 98/57 counts):
pick 228 (UEL, Daily) **won**, pick 779 (Tottenham v Coventry Over 2.5, Daily) **won**, picks 138–142 (DC United v
NY Red Bulls) **5 won**.

## B1.3 — Was the Oct-15 UEL void caused by the 60ee400 bug?

**Verdict: yes, it is the same short-name/full-name identity gap. But 60ee400 did not fix it, and the post-60ee400
code still fails to match this game today.**

Evidence (`analysis/uel_check.py`, output in `analysis/data/uel_check_output.txt`, using the current post-60ee400
code):

1. The void is pick **228**: Daily, `result3` / `away` (Sunderland) @ 1.323, prob 0.739, kickoff
   2026-10-15T16:45Z, league "UEFA Europa League" (resolves to `uel`).
2. football-data.org has no UEL on this plan (`uel.football_data = ""`), so ESPN (`soccer/uefa.europa`) is the only
   free path.
3. ESPN has the match, final, at the exact kickoff: home `displayName/shortDisplayName/name/location = "Torreense"`,
   away `"Sunderland"`, score **1–2**.
4. With our stored names, `ESPNProvider.find_result(...)` returns **None**. The away side matches. The home side
   does not: `same_team("Uniao Torreense", "Torreense")` is False, with `sport=''` and with `sport='football'`.
   Similarity is 0.750, under the 0.82 threshold, and the "one name's words are a subset of the other's" rule needs
   both names to have more than one word.
5. With the home side stored as ESPN spells it, the same call returns `final`, full time (1.0, 2.0). The pick would
   have graded **won**.
6. What 60ee400 changed: it only added `FOOTBALL_ALIASES = {"tottenham": "tottenham hotspur", "coventry": "coventry
   city"}` and applied them in `canonical_team()` when `sport == "football"`. That function is used for fixture
   identity. It does not help this case, for two reasons:
   - (a) there is no Torreense entry;
   - (b) the result matchers (`espn._matches`, `football_data._team_matches`, `euroleague._matches`) call
     `same_team(name, n)` without `sport`, so the football alias table is never consulted during settlement.

   An in-memory test shows that adding `"uniao torreense" -> "torreense"` makes `same_team(..., sport='football')`
   True but leaves `same_team(...)` (no sport, which is how settlement calls it) False.
7. The commit's own case behaves the same way in settlement. Pick 779 (Tottenham v Coventry, 2026-10-19) is in this
   void set, and today: `same_team("Tottenham", "Tottenham Hotspur")` is False and
   `same_team("Coventry", "Coventry City FC")` is False. Both are True with `sport='football'`. So 60ee400 fixed
   duplicate fixtures but not result matching for these names.

So the root cause is the same family of bug: provider short form vs our stored full form, or the other way round.
The 60ee400 fix covers neither this club nor the settlement code path.

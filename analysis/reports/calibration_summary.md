# Model calibration: Top and Daily picks (plus all published predictions)

Generated 2026-10-23 from production D1 (read-only SELECTs). Scripts: `analysis/pull_data.py`,
`analysis/grade_voids.py`, `analysis/calibration.py`. Raw data: `analysis/data/` (git-ignored). Every number
below comes from `analysis/reports/calibration_numbers.json` or the generated tables further down.

> **Read this first: the sample is small.** Production history starts on **2026-10-14**, which is about 10 days of
> data. Top has only **16** settled picks (18 with graded voids) and Daily has **35** (40). Both are below 50, so
> nothing below says anything about Top or Daily on its own. Picks also cluster on the same matches: the 233-pick
> set covers only 83 fixtures. The intervals below resample whole fixtures for that reason.

## Summary for the owner

1. **The model's probabilities are, in practice, the market's probabilities.** On the 190 binary-outcome picks with
   a result (82 fixtures, voids included), the model and the de-vigged market score the same: Brier 0.2117 vs
   0.2115, log loss 0.6095 vs 0.6092. The difference is +0.0002, with a 90% interval of [−0.0004, +0.0011]. On the
   56 picks that had a sharp-book reference, the model's like-for-like probability sat on average 0.08 points away
   from the sharp fair probability (mean absolute gap 0.98 points). There is **no segment with 50 or more picks
   where the model clearly beats the market.** It also clearly loses nowhere: the "worse" flags in section 5 are
   differences in the third or fourth decimal place.
2. **The model does not claim an edge on what it publishes.** Only **2 of 67** Top/Daily picks, and 8 of 1,375
   published predictions overall, had a positive model EV at the published price. Average EV is −3.4% for
   Top/Daily and −4.5% for all predictions. These products are "likely outcomes at bookmaker prices", not value
   bets. The +8.0% realised flat-stake return on the 55 Top/Daily picks (voids included) is not something the model
   predicted. Across all 233 scored predictions the realised return is +1.0%, against a model expectation of −4.3%.
3. **The "worse than market" result on all published predictions is mostly a scoring-convention artefact, not a
   model failure.** Using the published `prob`, the 233 picks score worse than `p_market` (Brier 0.2138 vs 0.2078).
   The gap comes from the 43 picks on lines that can push or half-settle (DNB, whole-number and quarter
   totals/handicaps):
   - The published `prob` on those lines is a *strict-win* probability, so pushes count against it. `p_market` is
     a decisive-stake price probability.
   - Scored like-for-like, using the model's own chance of winning given the bet is decided, the model is not
     worse. Brier on all 233 picks is 0.2064 vs 0.2078; the difference is −0.0014, 90% interval [−0.0027, 0.0000].
   - Practical point: on DNB and Asian lines, the confidence shown to users understates how often the bet wins when
     it is decided.
4. **Picks have won more often than predicted, but not enough to rule out luck.** Across all 233 scored
   predictions, the actual win rate is 65.2%. The mean published probability is 58.9%, and the like-for-like
   probability is 61.4%. That is +3.8 points over like-for-like, 90% interval [−1.1, +8.7], so zero is still
   plausible. Top/Daily on its own (55 picks with voids) shows 70.9% won against 63.0% predicted. Its interval for
   the like-for-like gap, [−2.0, +17.4] points, also includes zero.
5. **Much of the evaluation data is unsettled and could be recovered for free.** 1,108 published predictions are
   still `pending`; 652 of them kicked off before 2026-10-21, and none of those 652 is a Top or Daily pick. The 155 voids
   graded here (won/lost) more than triple the scored set for all predictions, from 78 to 233. They also exposed
   fixable settlement gaps: team-name mismatches (UEL Torreense, Tottenham/Coventry, NY Red Bulls), a
   competition-lookup gap for Odds-API Championship picks, and women's EuroLeague games labelled "Euroleague".
   Settling the backlog is the cheapest way to get a sample large enough to judge Top and Daily.

## Data and definitions

- **Population:** every pick with a `prediction_memberships` row (1,375 picks, ids 1–1375, pick dates 2026-10-14 …
  2026-10-23). Top/Daily = collection `top` or `daily` (67 picks).
  - Status counts (all picks): won 54, lost 24, half_lost 1, void 188, pending 1,108.
  - Top/Daily statuses: won 33, lost 15, void 11, pending 8.
- **Model probability `prob`:** the published `Scored.p_model` (`daily_picks.prediction_row()`: `'prob': s.p_model`).
  It is the ensemble blend after calibration and payoff conversion, stated as a **strict-win** probability
  (`sources._notes.probability_basis = strict_win_v2`). For every one of the 1,375 picks it equals
  `sources._notes.settlement.won`.
- **Baseline `p_market`:** `Scored.p_market` (`ensemble.py _market_probs()`). It is the de-vigged soft-market
  consensus at publication:
  - the books' mean price per selection, de-vigged across the complete market;
  - a single-side fallback when the market is incomplete;
  - it is a decisive-stake *price* probability, not converted to strict-win.

  `p_market` was present on **all** scored picks, so **0 picks were excluded** from the baseline comparison.
- **Sharp reference `p_fair`:** the Shin de-vig of sharp book(s) that quote every selection of the market. It is
  only present when such a book existed.
- **Like-for-like model probability ("cond."):** `won / (won + lost)` from the model's own settlement
  distribution (`sources._notes.settlement`). This is P(win | the bet ends won or lost), which matches a sample
  scored on won/lost only. It equals `prob` on binary lines.
- **Scoring:** won = 1, lost = 0.
  - push, void and not-obtainable are excluded from probability scoring.
  - **half_won / half_lost are excluded too**, because `prob` is a strict-win probability and a half result is
    neither a strict win nor a full loss. Only 1 exists, a half_lost in the `all` collection, so the exclusion is
    immaterial.
  - Brier = mean((p−y)²). Log loss uses p clipped to [1e-6, 1−1e-6].
  - "±SE" in the tables is the per-pick paired standard error. The bootstrap rows resample whole fixtures (2,000
    resamples, seed 7).
- **Graded voids:** the B1 in-memory grades of the 186 "settlement deadline expired" voids. Their 155 won/lost
  grades (98 won, 57 lost) form the separately flagged `graded_void` group; their 4 pushes and 27 not-obtainable
  picks are excluded. Only **7** of them are Top/Daily picks: 2 Top (both won) and 5 Daily (4 won, 1 lost). Every
  table is shown without and with them.
- **Two scopes:**
  - **Top + Daily**, the scope that was asked for.
  - **All published predictions**, which adds the `all` collection. This is supplementary and is the only scope
    with segments of 50 or more. Findings 1, 3 and 4 rest on it, and the summary says so.

## Section map (requirements 2–6)

- **2. Calibration tables:** "Calibration (model probability bins)" in each block. Each block also has a
  binary-lines-only version, where `prob` is directly comparable to won/lost.
- **3. Brier and log loss:** "Brier / log loss — model vs market baseline", overall and by product, sport, market
  and quality. Columns show the model (raw and like-for-like) and `p_market` on the same picks.
  `market_strict_win` (the market probability converted to strict-win) was also checked. On every segment of 50 or
  more it is within 0.001 Brier of the published `prob`, so the model and the market agree in strict-win terms too.
  `market_strict_win` is in `analysis/data/calibration_dataset.csv`.
- **4. Model vs sharp fair probability:** "Model vs sharp fair probability", split into all lines raw, all lines
  like-for-like, binary lines, and push-capable lines.
  - All published, with voids (n = 56, 32 fixtures): published prob averages −2.62 points against `p_fair`, so it
    looks less confident than sharp. Like-for-like the gap is +0.08 points, slightly more confident, with the model
    above sharp on 32% of picks.
  - Realised win rate was 64.3%, against 56.0% for the model (like-for-like) and 55.9% for sharp. Both
    under-predicted.
  - Top/Daily: 19 picks without voids, 22 with. Too small to conclude.
- **5. Segments worse than the market:** listed in "Segments where the model scores WORSE than the p_market
  baseline" at the end of the tables.
  - Segments of 50 or more flagged on the like-for-like basis: the binary-line segments (+0.0001 to +0.0007 Brier)
    and the Top+Daily overall-with-voids row (n = 55, +0.0008 Brier, 90% interval [−0.0014, +0.0035]). None of
    these differences is distinguishable from zero.
  - Flagged on the raw basis only: all-published overall, football, `main` quality and totals. These turn into
    "not worse" once scored like-for-like (see finding 3).
  - Everything under 50 is labelled "too small to conclude".
- **6. Sample caveat:** about 10 days of history, starting 2026-10-14. See the box at the top.

"""B2 - calibration of the published model probability vs the market baseline, from real production data.

Inputs (both produced by the other analysis scripts, never by hand):
  analysis/data/picks_raw.json   - every published prediction pulled read-only from production D1 (pull_data.py)
  analysis/data/void_grades.csv  - in-memory grades of the "settlement deadline expired" voids (grade_voids.py)

Outputs:
  analysis/data/calibration_dataset.csv - one row per scored pick (both groups, both scopes)
  analysis/reports/calibration_tables.md - every table (the report body)
  analysis/reports/calibration_numbers.json - headline numbers, machine-readable

Scoring rules
  * outcome y: won = 1, lost = 0. push / void / not-obtainable are excluded from probability scoring.
  * half_won / half_lost are EXCLUDED too: the stored ``prob`` is a STRICT-WIN probability
    (sources._notes.probability_basis = strict_win_v2), and a half result is neither a strict win nor a full loss.
    They are counted and reported separately.
  * Brier = mean((p - y)^2); log loss = mean(-[y ln p + (1-y) ln(1-p)]), p clipped to [1e-6, 1-1e-6].
  * Model = ``prob`` (published). Baseline = ``p_market`` (de-vigged soft-market consensus at publication).
    Robustness baseline = ``market_strict_win`` (sources._notes.settlement.market_strict_win: the same market
    probability converted to strict-win terms, i.e. like-for-like with ``prob`` on lines that can push).
  * Group "settled" = settled in production; group "graded_void" = deadline-expired voids graded in memory (B1).
"""
from __future__ import annotations

import csv
import json
import math
import os
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "data", "picks_raw.json")
VOIDS = os.path.join(HERE, "data", "void_grades.csv")
DATASET = os.path.join(HERE, "data", "calibration_dataset.csv")
TABLES = os.path.join(HERE, "reports", "calibration_tables.md")
NUMBERS = os.path.join(HERE, "reports", "calibration_numbers.json")
MIN_N = 50
EPS = 1e-6


def clip(p):
    return min(1 - EPS, max(EPS, p))


def brier(ps, ys):
    return sum((p - y) ** 2 for p, y in zip(ps, ys)) / len(ys)


def logloss(ps, ys):
    return sum(-(y * math.log(clip(p)) + (1 - y) * math.log(1 - clip(p))) for p, y in zip(ps, ys)) / len(ys)


def paired_se(a, b):
    """Standard error of mean(a - b) for paired per-pick losses."""
    d = [x - y for x, y in zip(a, b)]
    n = len(d)
    if n < 2:
        return float("nan")
    m = sum(d) / n
    var = sum((x - m) ** 2 for x in d) / (n - 1)
    return math.sqrt(var / n)


def cluster_boot(rows, fn, reps=2000, seed=7, level=0.90):
    """Point value of fn(rows) and a percentile interval from resampling whole fixtures with replacement."""
    import random
    rng = random.Random(seed)
    by_fix = defaultdict(list)
    for r in rows:
        by_fix[r["fixture"]].append(r)
    keys = list(by_fix)
    vals = []
    for _ in range(reps):
        sample = [r for k in (rng.choice(keys) for _ in keys) for r in by_fix[k]]
        vals.append(fn(sample))
    vals.sort()
    lo = vals[int((1 - level) / 2 * reps)]
    hi = vals[int((1 + level) / 2 * reps) - 1]
    return fn(rows), lo, hi


def load():
    with open(RAW, encoding="utf-8") as f:
        raw = json.load(f)
    with open(VOIDS, encoding="utf-8") as f:
        vg = {int(r["pick_id"]): r for r in csv.DictReader(f)}
    picks, half = [], defaultdict(int)
    for r in raw:
        cols = set((r.get("collections") or "").split(","))
        try:
            src = json.loads(r["sources"]) if r["sources"] else {}
        except ValueError:
            src = {}
        settle = ((src.get("_notes") or {}).get("settlement") or {})
        base = {
            "pick_id": r["id"], "pick_date": r["pick_date"], "sport": r["sport"], "league": r["league"],
            "home": r["home"], "away": r["away"], "market": r["market"], "period": r["period"],
            "selection": r["selection"], "line": r["line"], "price": r["price"], "prob": r["prob"],
            "p_market": r["p_market"], "p_fair": r["p_fair"], "market_strict_win": settle.get("market_strict_win"),
            "push_mass": (settle.get("push") or 0) + (settle.get("half_won") or 0) + (settle.get("half_lost") or 0),
            # the model's own P(won | the pick ends won or lost) - like-for-like with a won/lost-only sample
            "prob_cond": (settle["won"] / (settle["won"] + settle["lost"])
                          if settle.get("won") is not None and settle.get("lost") is not None
                          and settle["won"] + settle["lost"] > 0 else r["prob"]),
            "ev": r["ev"], "quality": r["quality"], "fixture": r["fixture_key"] or f"{r['home']}|{r['away']}|{r['start_utc'][:10]}",
            "in_top": "top" in cols, "in_daily": "daily" in cols, "in_all": "all" in cols,
        }
        if r["status"] in ("won", "lost"):
            picks.append({**base, "y": 1 if r["status"] == "won" else 0, "group": "settled"})
        elif r["status"] in ("half_won", "half_lost"):
            half[(r["status"], "top/daily" if (base["in_top"] or base["in_daily"]) else "all-only")] += 1
        elif r["status"] == "void" and r["id"] in vg and vg[r["id"]]["graded_outcome"] in ("won", "lost"):
            picks.append({**base, "y": 1 if vg[r["id"]]["graded_outcome"] == "won" else 0, "group": "graded_void",
                          "grading_source": vg[r["id"]]["grading_source"]})
    return picks, half


def metrics(rows):
    ys = [r["y"] for r in rows]
    out = {"n": len(rows), "fixtures": len({r["fixture"] for r in rows}), "wins": sum(ys)}
    if not rows:
        return out
    out["win_rate"] = sum(ys) / len(ys)
    out["mean_prob"] = sum(r["prob"] for r in rows) / len(rows)
    out["brier_model"] = brier([r["prob"] for r in rows], ys)
    out["ll_model"] = logloss([r["prob"] for r in rows], ys)
    out["brier_cond"] = brier([r["prob_cond"] for r in rows], ys)
    out["ll_cond"] = logloss([r["prob_cond"] for r in rows], ys)
    out["mean_prob_cond"] = sum(r["prob_cond"] for r in rows) / len(rows)
    out["roi"] = sum((r["price"] - 1) if r["y"] else -1 for r in rows) / len(rows)
    out["mean_ev"] = sum(r["ev"] for r in rows) / len(rows)
    mk = [r for r in rows if r["p_market"] is not None]
    out["n_market"] = len(mk)
    if mk:
        ym = [r["y"] for r in mk]
        bm = [(r["prob"] - r["y"]) ** 2 for r in mk]
        bb = [(r["p_market"] - r["y"]) ** 2 for r in mk]
        out["mean_p_market"] = sum(r["p_market"] for r in mk) / len(mk)
        out["brier_model_on_mkt"] = brier([r["prob"] for r in mk], ym)
        out["brier_market"] = brier([r["p_market"] for r in mk], ym)
        out["ll_model_on_mkt"] = logloss([r["prob"] for r in mk], ym)
        out["ll_market"] = logloss([r["p_market"] for r in mk], ym)
        out["brier_diff"] = out["brier_model_on_mkt"] - out["brier_market"]
        out["brier_diff_se"] = paired_se(bm, bb)
        bc = [(r["prob_cond"] - r["y"]) ** 2 for r in mk]
        out["brier_cond_on_mkt"] = brier([r["prob_cond"] for r in mk], ym)
        out["ll_cond_on_mkt"] = logloss([r["prob_cond"] for r in mk], ym)
        out["brier_cond_diff"] = out["brier_cond_on_mkt"] - out["brier_market"]
        out["brier_cond_diff_se"] = paired_se(bc, bb)
    sw = [r for r in rows if r["market_strict_win"] is not None]
    out["n_msw"] = len(sw)
    if sw:
        ys2 = [r["y"] for r in sw]
        out["brier_msw"] = brier([r["market_strict_win"] for r in sw], ys2)
        out["ll_msw"] = logloss([r["market_strict_win"] for r in sw], ys2)
        out["brier_model_on_msw"] = brier([r["prob"] for r in sw], ys2)
        out["ll_model_on_msw"] = logloss([r["prob"] for r in sw], ys2)
    return out


def fmt(x, d=3):
    return "-" if x is None or (isinstance(x, float) and math.isnan(x)) else f"{x:.{d}f}"


def pct(x):
    return "-" if x is None else f"{100 * x:.1f}%"


def size_label(n):
    return f"{n}" + (" (too small to conclude)" if n < MIN_N else "")


def worse_flag(m, cond=False):
    if "brier_market" not in m:
        return "no baseline"
    b, l = ("brier_cond_on_mkt", "ll_cond_on_mkt") if cond else ("brier_model_on_mkt", "ll_model_on_mkt")
    worse = []
    if m[b] > m["brier_market"]:
        worse.append("Brier")
    if m[l] > m["ll_market"]:
        worse.append("LL")
    return ("**WORSE (" + "+".join(worse) + ")**") if worse else "not worse"


SCORE_HEAD = ("| Segment | n | fixtures | win rate | mean prob | Brier model | Brier p_market | Δ Brier (±SE) | "
              "LogLoss model | LogLoss p_market | verdict (raw prob) | Brier model cond. | LL model cond. | "
              "Δ Brier cond. (±SE) | verdict (like-for-like) |\n"
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")


def score_row(name, m):
    if not m["n"]:
        return f"| {name} | 0 | | | | | | | | | | | | | |"
    return (f"| {name} | {size_label(m['n'])} | {m['fixtures']} | {pct(m['win_rate'])} | {pct(m['mean_prob'])} | "
            f"{fmt(m['brier_model'])} | {fmt(m.get('brier_market'))} | "
            f"{fmt(m.get('brier_diff'), 4)} (±{fmt(m.get('brier_diff_se'), 4)}) | {fmt(m['ll_model'])} | "
            f"{fmt(m.get('ll_market'))} | {worse_flag(m)} | {fmt(m['brier_cond'])} | {fmt(m['ll_cond'])} | "
            f"{fmt(m.get('brier_cond_diff'), 4)} (±{fmt(m.get('brier_cond_diff_se'), 4)}) | {worse_flag(m, True)} |")


BINS = [(0.0, 0.5, "<50"), (0.5, 0.55, "50–55"), (0.55, 0.6, "55–60"), (0.6, 0.65, "60–65"),
        (0.65, 0.7, "65–70"), (0.7, 0.8, "70–80"), (0.8, 1.01, "80+")]


def calib_table(rows):
    lines = ["| Model prob bin | n | mean model prob | mean p_market | actual win rate | gap (actual − model) |",
             "|---|---|---|---|---|---|"]
    for lo, hi, name in BINS:
        b = [r for r in rows if lo <= r["prob"] < hi]
        if not b:
            lines.append(f"| {name} | 0 | | | | |")
            continue
        mp = sum(r["prob"] for r in b) / len(b)
        mm = sum(r["p_market"] for r in b if r["p_market"] is not None) / max(1, sum(r["p_market"] is not None for r in b))
        wr = sum(r["y"] for r in b) / len(b)
        lines.append(f"| {name} | {size_label(len(b))} | {pct(mp)} | {pct(mm)} | {pct(wr)} | {100 * (wr - mp):+.1f} pts |")
    return "\n".join(lines)


def sharp_block(rows, field="prob", label="all lines, published prob"):
    s = [r for r in rows if r["p_fair"] is not None]
    if not s:
        return f"| {label} | 0 | | | | | | | | | | | | |", {}
    ys = [r["y"] for r in s]
    gaps = [r[field] - r["p_fair"] for r in s]
    above = sum(g > 0 for g in gaps)
    res = {"n": len(s), "fixtures": len({r["fixture"] for r in s}), "mean_gap": sum(gaps) / len(s),
           "share_model_higher": above / len(s), "mean_prob": sum(r[field] for r in s) / len(s),
           "mean_p_fair": sum(r["p_fair"] for r in s) / len(s), "win_rate": sum(ys) / len(s),
           "brier_model": brier([r[field] for r in s], ys), "brier_fair": brier([r["p_fair"] for r in s], ys),
           "ll_model": logloss([r[field] for r in s], ys), "ll_fair": logloss([r["p_fair"] for r in s], ys),
           "mean_abs_gap": sum(abs(g) for g in gaps) / len(s)}
    lean = "MORE confident than sharp" if res["mean_gap"] > 0 else "LESS confident than sharp"
    txt = (f"| {label} | {size_label(res['n'])} | {res['fixtures']} | {100 * res['mean_gap']:+.2f} pts | {100 * res['mean_abs_gap']:.2f} pts | "
           f"{res['share_model_higher'] * 100:.0f}% of picks | {lean} | {pct(res['mean_prob'])} | {pct(res['mean_p_fair'])} | "
           f"{pct(res['win_rate'])} | {fmt(res['brier_model'])} | {fmt(res['brier_fair'])} | {fmt(res['ll_model'])} | {fmt(res['ll_fair'])} |")
    return txt, res


SHARP_HEAD = ("| Subset | n | fixtures | mean (model − p_fair) | mean abs gap | model > sharp on | lean | mean model prob | "
              "mean p_fair | actual win rate | Brier model | Brier p_fair | LogLoss model | LogLoss p_fair |\n"
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")


def segments(rows, product_scope):
    out = [("**Overall**", rows)]
    if product_scope:
        out += [("product: top", [r for r in rows if r["in_top"]]), ("product: daily", [r for r in rows if r["in_daily"]])]
    else:
        out += [("product: top", [r for r in rows if r["in_top"]]), ("product: daily", [r for r in rows if r["in_daily"]]),
                ("product: all-only (not top/daily)", [r for r in rows if not (r["in_top"] or r["in_daily"])])]
    for key, label in (("sport", "sport"), ("market", "market"), ("quality", "quality")):
        for v in sorted({r[key] for r in rows}, key=lambda x: (-sum(r[key] == x for r in rows), str(x))):
            out.append((f"{label}: {v}", [r for r in rows if r[key] == v]))
    out.append(("line can push/half (push mass > 0)", [r for r in rows if r["push_mass"] > 0.001]))
    out.append(("binary line (no push mass)", [r for r in rows if r["push_mass"] <= 0.001]))
    return out


def main():
    picks, half = load()
    with open(DATASET, "w", encoding="utf-8", newline="") as f:
        keys = ["pick_id", "group", "grading_source", "pick_date", "sport", "league", "home", "away", "market",
                "period", "selection", "line", "price", "prob", "p_market", "market_strict_win", "p_fair", "prob_cond", "push_mass",
                "ev", "quality", "in_top", "in_daily", "in_all", "fixture", "y"]
        w = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(picks, key=lambda r: r["pick_id"]))

    scopes = {
        "td": ("Top + Daily (the products)", [r for r in picks if r["in_top"] or r["in_daily"]], True),
        "all": ("All published predictions (any membership, incl. the 'all' collection) - SUPPLEMENTARY",
                picks, False),
    }
    md, numbers = [], {"half_results_excluded": {f"{k[0]}|{k[1]}": v for k, v in half.items()}}
    for sk, (title, rows, prod) in scopes.items():
        numbers[sk] = {}
        md.append(f"\n## Scope: {title}\n")
        for gk, glabel, grows in (("without_voids", "Production-settled only (WITHOUT graded voids)",
                                   [r for r in rows if r["group"] == "settled"]),
                                  ("with_voids", "WITH graded voids added (settled + graded_void)", rows),
                                  ("voids_only", "Graded voids only (for reference)",
                                   [r for r in rows if r["group"] == "graded_void"])):
            ov = metrics(grows)
            numbers[sk][gk] = {"overall": ov}
            md.append(f"\n### {glabel} — n = {ov['n']} picks on {ov.get('fixtures', 0)} distinct fixtures\n")
            if not grows:
                md.append("_none_\n")
                continue
            md.append("\n#### Calibration (model probability bins)\n")
            md.append(calib_table(grows))
            md.append("\n\nSame bins, binary lines only (no push/half mass, so the published prob IS P(won | won or lost)):\n")
            md.append(calib_table([r for r in grows if r["push_mass"] <= 0.001]))
            md.append("\n\n#### Brier / log loss — model vs market baseline (same picks)\n")
            md.append(f"p_market missing on {ov['n'] - ov['n_market']} of {ov['n']} picks (excluded from the baseline "
                      f"columns only). market_strict_win missing on {ov['n'] - ov['n_msw']}.\n")
            md.append(SCORE_HEAD)
            seg_numbers = {}
            for name, srows in segments(grows, prod):
                m = metrics(srows)
                seg_numbers[name] = m
                md.append(score_row(name, m))
            numbers[sk][gk]["segments"] = seg_numbers
            md.append("\n\n#### Flat 1-unit stake result (realised, at the published price)\n")
            md.append("| Segment | n | realised ROI per pick | mean published EV |\n|---|---|---|---|")
            for name, srows in segments(grows, prod)[:3 if prod else 4]:
                m = seg_numbers[name]
                if m["n"]:
                    md.append(f"| {name} | {size_label(m['n'])} | {100 * m['roi']:+.1f}% | {100 * m['mean_ev']:+.1f}% |")
            md.append("\n\n#### Model vs sharp fair probability (picks with p_fair)\n")
            md.append(SHARP_HEAD)
            numbers[sk][gk]["sharp"] = {}
            for key, field, lab, sub in (
                    ("all_raw", "prob", "all lines, published prob", grows),
                    ("all_cond", "prob_cond", "all lines, model cond. prob (like-for-like)", grows),
                    ("binary", "prob", "binary lines only (no push mass)", [r for r in grows if r["push_mass"] <= 0.001]),
                    ("push", "prob_cond", "push-capable lines, model cond. prob", [r for r in grows if r["push_mass"] > 0.001])):
                txt, sres = sharp_block(sub, field, lab)
                numbers[sk][gk]["sharp"][key] = sres
                md.append(txt)
            md.append("")
            if grows:
                md.append("\n#### Fixture-clustered bootstrap (2,000 resamples of whole fixtures, seed 7) — 90% intervals\n")
                md.append("Several picks share one match, so picks are not independent; resampling whole fixtures gives "
                          "honest intervals.\n")
                md.append("| Quantity | subset | n picks | fixtures | point | 90% interval |\n|---|---|---|---|---|---|")
                boots = {}
                for bk, blab, sub in (("all", "all lines", grows),
                                      ("binary", "binary lines", [r for r in grows if r["push_mass"] <= 0.001])):
                    if not sub:
                        continue
                    for qk, qlab, fn in (
                            ("dbrier_cond", "Δ Brier, model cond. − p_market (negative = model better)",
                             lambda rs: brier([r["prob_cond"] for r in rs], [r["y"] for r in rs])
                             - brier([r["p_market"] for r in rs], [r["y"] for r in rs])),
                            ("calib_gap","win rate − mean model cond. prob (positive = model under-confident)",
                             lambda rs: sum(r["y"] for r in rs) / len(rs) - sum(r["prob_cond"] for r in rs) / len(rs))):
                        pt, lo, hi = cluster_boot(sub, fn)
                        boots[f"{bk}:{qk}"] = {"point": pt, "lo": lo, "hi": hi, "n": len(sub)}
                        md.append(f"| {qlab} | {blab} | {len(sub)} | {len({r['fixture'] for r in sub})} | "
                                  f"{pt:+.4f} | [{lo:+.4f}, {hi:+.4f}] |")
                numbers[sk][gk]["bootstrap"] = boots
                md.append("")
    md.append("\n## Segments where the model scores WORSE than the p_market baseline\n")
    md.append("Every segment from the tables above with a higher Brier or log loss than p_market. 'raw' = published prob; "
              "'like-for-like' = the model's own P(won | won or lost). Segments under 50 picks are too small to conclude.\n")
    md.append("| Scope | Group | Segment | n | raw verdict | like-for-like verdict |\n|---|---|---|---|---|---|")
    for sk, (title, _rows, _prod) in scopes.items():
        for gk in ("without_voids", "with_voids", "voids_only"):
            for name, m in (numbers[sk].get(gk, {}).get("segments") or {}).items():
                if not m.get("n") or "brier_market" not in m:
                    continue
                raw_v, cond_v = worse_flag(m), worse_flag(m, True)
                if "WORSE" in raw_v or "WORSE" in cond_v:
                    md.append(f"| {sk} | {gk} | {name} | {size_label(m['n'])} | {raw_v} | {cond_v} |")
    raw_all = json.load(open(RAW, encoding="utf-8"))
    td_all = [r for r in raw_all if {"top", "daily"} & set((r.get("collections") or "").split(","))]
    numbers["ev_sign"] = {
        "top_daily_published": len(td_all), "top_daily_ev_positive": sum(r["ev"] > 0 for r in td_all),
        "top_daily_mean_ev": sum(r["ev"] for r in td_all) / len(td_all),
        "all_published": len(raw_all), "all_ev_positive": sum(r["ev"] > 0 for r in raw_all),
        "all_mean_ev": sum(r["ev"] for r in raw_all) / len(raw_all)}
    e = numbers["ev_sign"]
    md.append("\n## Published EV sign (every published pick, any status)\n")
    md.append(f"Top/Daily: {e['top_daily_ev_positive']} of {e['top_daily_published']} picks had a positive model EV at the "
              f"published price (mean EV {100 * e['top_daily_mean_ev']:+.1f}%). All published predictions: "
              f"{e['all_ev_positive']} of {e['all_published']} (mean EV {100 * e['all_mean_ev']:+.1f}%).\n")
    with open(TABLES, "w", encoding="utf-8") as f:
        f.write("\n".join(md) + "\n")
    # calibration.md = the hand-written summary/method (reports/calibration_summary.md, whose every number is copied
    # from calibration_numbers.json / the tables) followed by the generated tables
    summary = os.path.join(HERE, "reports", "calibration_summary.md")
    if os.path.exists(summary):
        with open(summary, encoding="utf-8") as f:
            head = f.read()
        with open(os.path.join(HERE, "reports", "calibration.md"), "w", encoding="utf-8") as f:
            f.write(head.rstrip() + "\n\n---\n\n# Full tables (generated by analysis/calibration.py)\n" + "\n".join(md) + "\n")
    with open(NUMBERS, "w", encoding="utf-8") as f:
        json.dump(numbers, f, indent=1, default=str)
    print(f"scored picks: {len(picks)} (settled {sum(r['group'] == 'settled' for r in picks)}, "
          f"graded_void {sum(r['group'] == 'graded_void' for r in picks)}); half results excluded: {dict(half)}")
    print(f"wrote {DATASET}\nwrote {TABLES}\nwrote {NUMBERS}")


if __name__ == "__main__":
    main()

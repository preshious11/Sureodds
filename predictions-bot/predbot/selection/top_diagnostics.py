"""Secret-free operator configuration for the Top pipeline."""
import os

from predbot.config import is_secret


def effective_config(cfg):
    names = ("top_min_prob", "top_relax_prob", "top_min_books", "top_min_agreement",
             "top_max_neg_ev", "top_leg_odds_min", "top_legs_min", "top_legs_max",
             "top_odds_target", "top_odds_max", "top_relaxed_fallback", "top_relax_band",
             "min_edge_vs_sharp", "edge_price", "max_book_dispersion", "max_unsupported_edge",
             "rank_z", "se_main", "se_no_sharp", "allowed_sports", "disabled_markets",
             "min_lead_minutes", "pick_window_hours", "day_window_end_offset_hours",
             "top_provider_priority", "sharpapi_leagues", "odds_api_league_allowlist", "sgo_leagues")
    return {**{name: getattr(cfg, name) for name in names},
            "environment_values": {k: v for k, v in os.environ.items()
                                   if not is_secret(k) and (k.startswith('TOP_') or k in
                                       ('MIN_EDGE_VS_SHARP', 'EDGE_PRICE', 'HONOR_PINNED_VALUES',
                                        'INTERNATIONAL_FOOTBALL', 'ALLOWED_SPORTS', 'DISABLED_MARKETS'))},
            "international_football": cfg.international_football,
            "strict_odds_range": [cfg.top_odds_target, cfg.top_odds_max],
            "fallback_odds_range": [cfg.top_odds_target - cfg.top_relax_band,
                                    cfg.top_odds_max + cfg.top_relax_band] if cfg.top_relaxed_fallback else None,
            "top_max_picks": cfg.top_legs_max, "market_quality": "main",
            "provider_minimum": None, "model_signal_minimum": 2, "sharp_required": False,
            "top_lcb_floor": None, "diversity": "tie-break only", "per_fixture": 1}

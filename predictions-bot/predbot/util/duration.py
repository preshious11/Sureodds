"""Typical match length per sport - one table for settlement (when to ask for a
result) and for the cards (whether a leg has probably finished)."""

from datetime import datetime, timedelta

DURATION_H = {"football": 2.2, "basketball": 2.8, "american_football": 3.6,
              "baseball": 3.6, "tennis": 3.0, "mma": 3.0, "other": 3.0}

# Time after expected completion during which live status is still useful.
# Longer tennis/baseball windows allow long matches, weather delays and extra innings.
LIVE_GRACE_H = {"football": 2.0, "basketball": 3.0, "american_football": 4.0,
                "baseball": 6.0, "tennis": 6.0, "mma": 3.0, "other": 3.0}
# Finalization can outlast live polling, but must never become an unlimited retry.
# Tennis/baseball allow an overnight interruption/resumption and delayed reporting.
SETTLEMENT_GRACE_H = {"football": 24.0, "basketball": 24.0, "american_football": 24.0,
                      "baseball": 48.0, "tennis": 48.0, "mma": 24.0, "other": 24.0}


def expected_end(start: datetime, sport: str) -> datetime:
    return start + timedelta(hours=DURATION_H.get(sport, DURATION_H["other"]))


def live_cutoff(start: datetime, sport: str) -> datetime:
    return expected_end(start, sport) + timedelta(hours=LIVE_GRACE_H.get(sport, LIVE_GRACE_H["other"]))


def settlement_deadline(start: datetime, sport: str) -> datetime:
    return expected_end(start, sport) + timedelta(hours=SETTLEMENT_GRACE_H.get(sport, SETTLEMENT_GRACE_H["other"]))

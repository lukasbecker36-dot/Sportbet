"""Betfair historical parser + team-name normalisation."""

import bz2
import os

import pytest

from scrapers.betfair_historical import extract_over_goals_prices, net_odds
from utils.teams import normalise


CSV = (
    "EVENT_NAME,MARKET_NAME,SELECTION_NAME,MARKET_TIME,LAST_PRICE_TRADED,MATCHED_AMOUNT\n"
    "Arsenal v Chelsea,Over/Under 2.5 Goals,Over 2.5 Goals,2024-01-15T15:30:00Z,2.10,1500.0\n"
    "Arsenal v Chelsea,Over/Under 2.5 Goals,Under 2.5 Goals,2024-01-15T15:30:00Z,1.85,1400.0\n"
    "Arsenal v Chelsea,Over/Under 1.5 Goals,Over 1.5 Goals,2024-01-15T15:30:00Z,1.40,900.0\n"
    "Arsenal v Chelsea,Match Odds,Arsenal,2024-01-15T15:30:00Z,1.90,5000.0\n"
)


def _write_bz2(path):
    with bz2.open(path, "wt") as fh:
        fh.write(CSV)


def test_extract_over_goals_only(tmp_path):
    p = os.path.join(tmp_path, "sample.csv.bz2")
    _write_bz2(p)
    rows = extract_over_goals_prices(p)
    lines = sorted(r["line"] for r in rows)
    assert lines == [1.5, 2.5]
    by_line = {r["line"]: r for r in rows}
    assert by_line[2.5]["last_price_traded"] == pytest.approx(2.10)
    assert by_line[2.5]["home"] == "arsenal"
    assert by_line[2.5]["away"] == "chelsea"
    assert by_line[2.5]["matched_amount"] == pytest.approx(1500.0)


def test_net_odds_applies_commission():
    # 2% commission on winnings: 3.0 -> (3-1)*0.98 + 1 = 2.96
    assert net_odds(3.0) == pytest.approx(2.96)
    assert net_odds(1.0) == pytest.approx(1.0)


def test_normalise_strips_suffixes_and_accents():
    assert normalise("Manchester United FC") == "manchester united"
    assert normalise("Atlético Madrid") == "atletico madrid"
    assert normalise("  Brighton & Hove Albion  ") == "brighton hove albion"

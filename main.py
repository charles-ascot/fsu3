"""
FSU3 — CHIMERA Standalone Backtest Service
===========================================
Runs backtests against FSU historic Betfair data independently of the Lay Engine.
Provides a REST API for integration with other CHIMERA units.

Endpoints:
  GET  /api/health              — Health check
  GET  /api/dates               — Available backtest dates (proxied from FSU)
  GET  /api/markets             — WIN markets for a date (proxied from FSU)
  POST /api/backtest/run        — Run a full-day backtest
  GET  /api/rules               — Current rule definitions
"""

import logging
import os
from datetime import datetime, timezone

import requests as _requests
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from fsu_client import FSUClient
from rules import apply_rules

load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("fsu3")

# ── Configuration ──

FSU_URL = os.environ.get("FSU_URL", "https://fsu.thync.online")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "*")

# ── FastAPI app ──

app = FastAPI(title="FSU3 — CHIMERA Backtest Service", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL] if FRONTEND_URL != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── GCP auth helper (Cloud Run → Cloud Run) ──

def _fsu_auth_header() -> dict:
    """
    Fetch a GCP OIDC identity token for service-to-service Cloud Run auth.
    Returns empty dict when running locally (no metadata server).
    """
    meta_url = (
        "http://metadata.google.internal/computeMetadata/v1/instance/"
        f"service-accounts/default/identity?audience={FSU_URL}"
    )
    try:
        resp = _requests.get(meta_url, headers={"Metadata-Flavor": "Google"}, timeout=3)
        if resp.status_code == 200:
            return {"Authorization": f"Bearer {resp.text.strip()}"}
    except Exception:
        pass
    return {}


# ── Health ──

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "fsu3", "fsu_url": FSU_URL}


# ── FSU proxy endpoints ──

@app.get("/api/dates")
def backtest_dates():
    """Return available backtest dates from the FSU service."""
    try:
        r = _requests.get(f"{FSU_URL}/api/dates", headers=_fsu_auth_header(), timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"FSU unavailable: {e}")


@app.get("/api/markets")
def backtest_markets(
    date: str = Query(..., description="YYYY-MM-DD"),
    countries: str = Query("GB,IE", description="Comma-separated country codes"),
):
    """Return WIN markets for a given date from the FSU."""
    try:
        r = _requests.get(
            f"{FSU_URL}/api/markets",
            params={"date": date, "market_type": "WIN", "countries": countries},
            headers=_fsu_auth_header(),
            timeout=30,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"FSU unavailable: {e}")


# ── Rules ──

@app.get("/api/rules")
def get_rules():
    """Return the current rule definitions."""
    from rules import MAX_LAY_ODDS, CLOSE_ODDS_THRESHOLD, SPREAD_THRESHOLDS
    return {
        "max_lay_odds": MAX_LAY_ODDS,
        "close_odds_threshold": CLOSE_ODDS_THRESHOLD,
        "spread_thresholds": [
            {"min_odds": lo, "max_odds": hi, "max_spread": sp}
            for lo, hi, sp in SPREAD_THRESHOLDS
        ],
        "rules": [
            {"rule": "RULE_1", "condition": "Fav odds < 2.0", "stake": "£3 lay on favourite"},
            {"rule": "RULE_1_JOINT", "condition": "Fav odds < 2.0, gap ≤ 0.2", "stake": "£1.50 fav + £1.50 2nd fav"},
            {"rule": "RULE_2", "condition": "Fav odds 2.0–5.0", "stake": "£2 lay on favourite"},
            {"rule": "RULE_2_JOINT", "condition": "Fav odds 2.0–5.0, gap ≤ 0.2", "stake": "£1.00 fav + £1.00 2nd fav"},
            {"rule": "RULE_2_UPLIFT", "condition": "Fav odds 2.5–3.5 (Mark)", "stake": "£5 lay on favourite"},
            {"rule": "RULE_3A", "condition": "Fav odds > 5.0, gap < 2", "stake": "£1 fav + £1 2nd fav"},
            {"rule": "RULE_3B", "condition": "Fav odds > 5.0, gap ≥ 2", "stake": "£1 lay on favourite"},
        ],
    }


# ── Backtest ──

class BacktestRunRequest(BaseModel):
    date: str
    countries: list[str] = ["GB", "IE"]
    process_window_mins: float = 5
    jofs_enabled: bool = True
    mark_ceiling_enabled: bool = False
    mark_floor_enabled: bool = False
    mark_uplift_enabled: bool = False
    market_ids: list[str] = []  # empty = run all markets for the date


@app.post("/api/backtest/run")
def backtest_run(req: BacktestRunRequest):
    """
    Run a full-day backtest against FSU historic Betfair data.
    Evaluates each market at race_time - process_window_mins, applies rules,
    then checks the final settlement to compute P&L.
    """
    client = FSUClient(base_url=FSU_URL, date=req.date)
    client.login()
    markets = client.get_todays_win_markets(countries=req.countries)
    if req.market_ids:
        markets = [m for m in markets if m["market_id"] in req.market_ids]

    if not markets:
        return {
            "date": req.date,
            "countries": req.countries,
            "process_window_mins": req.process_window_mins,
            "markets_evaluated": 0,
            "bets_placed": 0,
            "markets_skipped": 0,
            "total_stake": 0.0,
            "total_liability": 0.0,
            "total_pnl": 0.0,
            "roi": 0.0,
            "results": [],
        }

    results = []
    for m in markets:
        market_id = m["market_id"]
        race_time_str = m["race_time"]

        # Target evaluation time: race_time minus process window
        try:
            race_dt = datetime.fromisoformat(race_time_str.replace("Z", "+00:00"))
            target_ts = race_dt.timestamp() - (req.process_window_mins * 60)
            target_iso = datetime.fromtimestamp(target_ts, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except Exception:
            target_iso = race_time_str

        client.set_virtual_time(target_iso)
        runners, valid = client.get_market_prices(market_id)

        if not valid:
            results.append({
                "market_id": market_id,
                "market_name": m["market_name"],
                "venue": m["venue"],
                "race_time": race_time_str,
                "evaluated_at": target_iso,
                "skipped": True,
                "skip_reason": "Market not valid at evaluation time (closed / suspended / in-play)",
                "rule_applied": "",
                "favourite": None,
                "second_favourite": None,
                "instructions": [],
                "settled": False,
                "winner_selection_id": None,
                "pnl": 0.0,
                "total_stake": 0.0,
                "total_liability": 0.0,
            })
            continue

        rule_result = apply_rules(
            market_id=market_id,
            market_name=m["market_name"],
            venue=m["venue"],
            race_time=race_time_str,
            runners=runners,
            jofs_enabled=req.jofs_enabled,
            mark_ceiling_enabled=req.mark_ceiling_enabled,
            mark_floor_enabled=req.mark_floor_enabled,
            mark_uplift_enabled=req.mark_uplift_enabled,
        )

        if rule_result.skipped:
            rd = rule_result.to_dict()
            rd["evaluated_at"] = target_iso
            rd["settled"] = False
            rd["winner_selection_id"] = None
            rd["pnl"] = 0.0
            results.append(rd)
            continue

        # Determine race outcome
        race_result = client.get_race_result(market_id)
        winner_id = race_result.get("winner_selection_id") if race_result else None
        settled = race_result.get("settled", False) if race_result else False

        # Calculate P&L per instruction
        total_pnl = 0.0
        instructions_with_outcome = []
        for instr in rule_result.instructions:
            if not settled or winner_id is None:
                outcome = "UNSETTLED"
                instr_pnl = 0.0
            elif instr.selection_id == winner_id:
                outcome = "LOST"
                instr_pnl = -round(instr.size * (instr.price - 1), 2)
            else:
                outcome = "WON"
                instr_pnl = round(instr.size, 2)

            total_pnl += instr_pnl
            d = instr.to_dict()
            d["outcome"] = outcome
            d["pnl"] = instr_pnl
            instructions_with_outcome.append(d)

        rd = rule_result.to_dict()
        rd["instructions"] = instructions_with_outcome
        rd["evaluated_at"] = target_iso
        rd["winner_selection_id"] = winner_id
        rd["settled"] = settled
        rd["pnl"] = round(total_pnl, 2)
        results.append(rd)

    # Aggregate summary stats
    active_results = [r for r in results if not r.get("skipped")]
    total_stake = round(
        sum(sum(i.get("size", 0) for i in r.get("instructions", [])) for r in active_results), 2
    )
    total_liability = round(
        sum(sum(i.get("liability", 0) for i in r.get("instructions", [])) for r in active_results), 2
    )
    total_pnl = round(sum(r.get("pnl", 0) for r in results), 2)
    bets_placed = sum(len(r.get("instructions", [])) for r in active_results)

    return {
        "date": req.date,
        "countries": req.countries,
        "process_window_mins": req.process_window_mins,
        "markets_evaluated": len(markets),
        "bets_placed": bets_placed,
        "markets_skipped": sum(1 for r in results if r.get("skipped")),
        "total_stake": total_stake,
        "total_liability": total_liability,
        "total_pnl": total_pnl,
        "roi": round((total_pnl / total_stake * 100) if total_stake > 0 else 0.0, 1),
        "results": results,
    }

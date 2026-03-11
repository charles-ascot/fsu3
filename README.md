# FSU3 — CHIMERA Backtest Service

**Version:** 5.0.0
**Cloud Run:** `fsu3` (europe-west2) — deployed via `gcloud run deploy`
**GitHub:** `charles-ascot/fsu3`
**Project:** `chimera-v4`

## Purpose

FSU3 is the standalone backtest service for the CHIMERA platform. It runs full-day backtests against historic Betfair data by connecting to FSU1 (Data Replay Service), applying the complete CHIMERA rules engine, and computing P&L for each market.

Extracted from the Lay Engine to operate independently, FSU3 can be called by any CHIMERA unit or external system via its REST API.

## Architecture

```
FSU1 (Data Replay)
    │
    ▼
FSU3 (Backtest Service)
  ├── fsu_client.py     FSU client with virtual clock
  ├── rules.py          Full rules engine (4 rules + JOFS + Mark Rules + Spread Control)
  └── main.py           FastAPI REST API
```

### Backtest Flow

1. Receive backtest request (date, countries, rule settings)
2. Fetch WIN markets for the date from FSU1
3. For each market:
   - Set virtual clock to `race_time - process_window`
   - Fetch prices from FSU1 at that timestamp
   - Apply spread control (if enabled)
   - Apply rules engine to determine lay instructions
   - Apply point value multiplier
   - Fetch race result (final settlement) from FSU1
   - Calculate P&L per instruction (WON/LOST/UNSETTLED)
4. Return aggregated results with summary stats

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Health check + FSU URL |
| `GET` | `/api/dates` | Available backtest dates (proxied from FSU1) |
| `GET` | `/api/markets` | WIN markets for a date (proxied from FSU1) |
| `POST` | `/api/backtest/run` | Run a full-day backtest |
| `GET` | `/api/rules` | Current rule definitions and thresholds |

### POST /api/backtest/run

Request body:

```json
{
  "date": "2025-12-31",
  "countries": ["GB", "IE"],
  "process_window_mins": 5,
  "jofs_enabled": true,
  "spread_control": false,
  "mark_ceiling_enabled": true,
  "mark_floor_enabled": true,
  "mark_uplift_enabled": true,
  "mark_uplift_stake": 3.0,
  "point_value": 10.0,
  "market_ids": []
}
```

Response includes per-market results with favourite, rule applied, instructions (with outcome and P&L), and aggregate summary (total stake, liability, P&L, ROI).

## Rules Engine

| Band | Rule | Base Stake | Notes |
|------|------|------------|-------|
| < 1.5 | SKIP | — | Hard Floor (Mark Rule) |
| 1.5–2.0 | Rule 1 | 3 pts | JOFS splits to 1.50 + 1.50 |
| 2.0–2.5 | Rule 2 | 2 pts | JOFS splits to 1.00 + 1.00 |
| 2.5–3.5 | Rule 2 + Uplift | 3 pts (adjustable) | JOFS splits evenly |
| 3.5–5.0 | Rule 2 | 2 pts | JOFS splits to 1.00 + 1.00 |
| 5.0–8.0 | Rule 3A/3B | 1–2 pts | Gap-based dual/single lay |
| > 8.0 | SKIP | — | Hard Ceiling (Mark Rule) |

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `FSU_URL` | `https://fsu.thync.online` | FSU1 data replay service URL |
| `FRONTEND_URL` | `*` | Allowed CORS origin |

## Authentication

- **Cloud Run → FSU1:** GCP OIDC identity tokens via metadata server
- **IAM:** FSU3 service account needs `roles/run.invoker` on FSU1

## Deployment

```bash
gcloud run deploy fsu3 --source . --region=europe-west2 --project=chimera-v4 \
  --set-env-vars "FSU_URL=https://fsu1-950990732577.europe-west1.run.app"
```

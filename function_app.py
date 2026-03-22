"""
Scout — Azure Functions Entry Point (Python v2 model)
======================================================
All Azure Function triggers are registered here via a single FunctionApp instance.

Functions:
  - daily_sync   : Timer trigger — runs daily at 03:00 UTC
  - http_refresh : HTTP POST /api/refresh — manual backfill
"""

import json
import logging

import azure.functions as func

from sync_logic import run_sync

logger = logging.getLogger(__name__)
app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


# ── Timer trigger — daily at 03:00 UTC ───────────────────────────────────────

@app.timer_trigger(
    schedule="0 0 3 * * *",
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def daily_sync(timer: func.TimerRequest) -> None:
    """Runs every day at 03:00 UTC — syncs yesterday's matches into Supabase."""
    logger.info("daily_sync triggered (past_due=%s)", timer.past_due)
    result = run_sync(trigger="daily_timer")
    logger.info("daily_sync result: %s", result)


# ── HTTP trigger — manual backfill ────────────────────────────────────────────

@app.route(route="refresh", methods=["POST"])
def http_refresh(req: func.HttpRequest) -> func.HttpResponse:
    """
    POST /api/refresh
    Body (optional): { "day_offset": 0 }   # 0=yesterday (default), 1=two days ago
    Returns: { "fetched": N, "upserted": N, "errors": N, "duration_ms": N }
    """
    logger.info("http_refresh triggered")

    try:
        body = req.get_json()
    except Exception:
        body = {}

    day_offset = int((body or {}).get("day_offset", 0))
    result = run_sync(trigger="http", day_offset=day_offset)

    return func.HttpResponse(
        body=json.dumps(result),
        status_code=200,
        mimetype="application/json",
    )

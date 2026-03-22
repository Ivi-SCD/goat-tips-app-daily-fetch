"""
Azure Function — HTTP Refresh Trigger
======================================
Trigger:  HTTP POST /api/refresh
Purpose:  Manual or CI-triggered sync for a specific day.
          Useful for backfilling missed days or testing.

Body (optional JSON):
  { "day_offset": 0 }   # 0 = yesterday (default), 1 = two days ago, etc.

Returns:
  { "fetched": N, "upserted": N, "errors": N, "duration_ms": N }
"""

import json
import logging

import azure.functions as func

from azure_functions.daily_sync import run_sync

logger = logging.getLogger(__name__)


@func.http_trigger(
    route="refresh",
    methods=["POST"],
    auth_level=func.AuthLevel.FUNCTION,
)
def http_refresh(req: func.HttpRequest) -> func.HttpResponse:
    logger.info("http_refresh triggered")

    try:
        body = req.get_json()
    except Exception:
        body = {}

    day_offset = int(body.get("day_offset", 0)) if body else 0
    result = run_sync(trigger="http", day_offset=day_offset)

    return func.HttpResponse(
        body=json.dumps(result),
        status_code=200,
        mimetype="application/json",
    )

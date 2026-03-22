"""
Azure Function — Daily BetsAPI Sync
=====================================
Trigger:  Timer (runs daily at 03:00 UTC — after matches finish)
Purpose:  Fetch all ended Premier League matches from the previous day,
          upsert teams, events, stats, timeline and latest odds into Supabase.

Cron:  "0 0 3 * * *"  (Azure Timer format: second minute hour day month weekday)
"""

import json
import logging
import os
import time
from datetime import datetime, timezone, timedelta

import azure.functions as func
import httpx
import psycopg2
import psycopg2.extras

logger = logging.getLogger(__name__)

BETSAPI_BASE = "https://api.b365api.com"
TOKEN = os.environ["BETSAPI_TOKEN"]
DB_URL = os.environ["SUPABASE_DB_URL"]
LEAGUE_ID = int(os.environ.get("PREMIER_LEAGUE_ID", "94"))
SPORT_ID = 1


# ── BetsAPI helpers ───────────────────────────────────────────────────────────

def fetch_ended_events(day_offset: int = 0) -> list[dict]:
    """
    Fetch all ended events for league on a specific day.
    day_offset=0 → yesterday, -1 → two days ago, etc.
    """
    target_date = datetime.now(timezone.utc) - timedelta(days=1 + abs(day_offset))
    date_str = target_date.strftime("%Y%m%d")

    all_events = []
    page = 1
    while True:
        with httpx.Client(timeout=20) as client:
            r = client.get(f"{BETSAPI_BASE}/v1/events/ended", params={
                "token": TOKEN,
                "sport_id": SPORT_ID,
                "league_id": LEAGUE_ID,
                "day": date_str,
                "page": page,
            })
            r.raise_for_status()
            data = r.json()

        results = data.get("results", [])
        all_events.extend(results)

        pager = data.get("pager", {})
        total_pages = (pager.get("total", 0) + pager.get("per_page", 50) - 1) // pager.get("per_page", 50)
        if page >= total_pages or not results:
            break
        page += 1

    return all_events


def fetch_event_detail(event_id: str) -> dict:
    """Fetch full event details including stats and timeline."""
    with httpx.Client(timeout=15) as client:
        r = client.get(f"{BETSAPI_BASE}/v1/event/view", params={
            "token": TOKEN, "event_id": event_id
        })
        r.raise_for_status()
        data = r.json()
    results = data.get("results", [])
    return results[0] if results else {}


def fetch_event_stats(event_id: str) -> list[dict]:
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{BETSAPI_BASE}/v1/event/stats", params={
                "token": TOKEN, "event_id": event_id
            })
            if r.status_code != 200:
                return []
            return r.json().get("results", [])
    except Exception:
        return []


def fetch_event_odds(event_id: str) -> dict:
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get(f"{BETSAPI_BASE}/v2/event/odds/summary", params={
                "token": TOKEN, "event_id": event_id
            })
            if r.status_code != 200:
                return {}
            return r.json().get("results", {}).get("odds", {})
    except Exception:
        return {}


# ── DB helpers (sync psycopg2) ────────────────────────────────────────────────

def get_conn():
    return psycopg2.connect(DB_URL)


def upsert_team(cur, team: dict) -> None:
    cur.execute("""
        INSERT INTO teams (id, name, image_id, created_at, updated_at)
        VALUES (%(id)s, %(name)s, %(image_id)s, NOW(), NOW())
        ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name, updated_at = NOW()
    """, {
        "id": int(team.get("id", 0)),
        "name": team.get("name", "Unknown"),
        "image_id": team.get("image_id"),
    })


def upsert_event(cur, event: dict, detail: dict) -> None:
    extra = detail.get("extra", {}) or {}
    referee = (extra.get("referee") or {}).get("name")
    stadium = extra.get("stadium_data", {}) or {}

    def _si(v):
        try:
            return int(v) if v is not None else None
        except (ValueError, TypeError):
            return None

    time_unix = _si(event.get("time"))
    time_utc = None
    if time_unix:
        time_utc = datetime.fromtimestamp(time_unix, tz=timezone.utc)

    cur.execute("""
        INSERT INTO events (
            id, time_unix, time_utc, time_status, league_id, league_name,
            home_team_id, away_team_id, home_score, away_score, score_string,
            round, stadium_name, stadium_city, referee_name, bet365_id,
            created_at, updated_at
        ) VALUES (
            %(id)s,%(time_unix)s,%(time_utc)s,%(time_status)s,%(league_id)s,%(league_name)s,
            %(home_team_id)s,%(away_team_id)s,%(home_score)s,%(away_score)s,%(score_string)s,
            %(round)s,%(stadium_name)s,%(stadium_city)s,%(referee_name)s,%(bet365_id)s,
            NOW(),NOW()
        )
        ON CONFLICT (id) DO UPDATE SET
            time_status  = EXCLUDED.time_status,
            home_score   = EXCLUDED.home_score,
            away_score   = EXCLUDED.away_score,
            updated_at   = NOW()
    """, {
        "id": int(event["id"]),
        "time_unix": time_unix,
        "time_utc": time_utc,
        "time_status": _si(event.get("time_status")),
        "league_id": _si((event.get("league") or {}).get("id")) or LEAGUE_ID,
        "league_name": (event.get("league") or {}).get("name"),
        "home_team_id": _si((event.get("home") or {}).get("id")),
        "away_team_id": _si((event.get("away") or {}).get("id")),
        "home_score": _si((event.get("ss") or "0-0").split("-")[0]),
        "away_score": _si((event.get("ss") or "0-0").split("-")[1]),
        "score_string": event.get("ss"),
        "round": str(extra.get("round", "")) or None,
        "stadium_name": stadium.get("name"),
        "stadium_city": stadium.get("city"),
        "referee_name": referee,
        "bet365_id": event.get("bet365_id"),
    })


def upsert_stats(cur, event_id: int, stats_list: list) -> int:
    count = 0
    for s in stats_list:
        try:
            cur.execute("""
                INSERT INTO match_stats (event_id, metric, home_value, away_value, period, created_at)
                VALUES (%s, %s, %s, %s, %s, NOW())
                ON CONFLICT (event_id, metric, period) DO UPDATE SET
                    home_value = EXCLUDED.home_value,
                    away_value = EXCLUDED.away_value
            """, (
                event_id,
                s.get("type") or s.get("metric"),
                _to_num(s.get("home") or s.get("home_value")),
                _to_num(s.get("away") or s.get("away_value")),
                s.get("period", "full"),
            ))
            count += 1
        except Exception as exc:
            logger.warning("stat upsert error for event %s: %s", event_id, exc)
    return count


def upsert_odds(cur, event_id: int, odds: dict) -> None:
    for market_key, data in odds.items():
        if not isinstance(data, dict):
            continue
        try:
            cur.execute("""
                INSERT INTO odds_snapshots (
                    event_id, market_key, home_od, draw_od, away_od,
                    over_od, under_od, yes_od, no_od, recorded_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW())
                ON CONFLICT (event_id, market_key) DO UPDATE SET
                    home_od = EXCLUDED.home_od,
                    draw_od = EXCLUDED.draw_od,
                    away_od = EXCLUDED.away_od,
                    over_od = EXCLUDED.over_od,
                    recorded_at = NOW()
            """, (
                event_id, market_key,
                _to_num(data.get("home_od")),
                _to_num(data.get("draw_od")),
                _to_num(data.get("away_od")),
                _to_num(data.get("over_od")),
                _to_num(data.get("under_od")),
                _to_num(data.get("yes_od")),
                _to_num(data.get("no_od")),
            ))
        except Exception as exc:
            logger.warning("odds upsert error for event %s: %s", event_id, exc)


def log_sync_run(cur, trigger: str, fetched: int, upserted: int,
                 errors: int, duration_ms: int, notes: str = None) -> None:
    cur.execute("""
        INSERT INTO sync_log (run_at, trigger, events_fetched, events_upserted,
                              errors, duration_ms, notes)
        VALUES (NOW(), %s, %s, %s, %s, %s, %s)
    """, (trigger, fetched, upserted, errors, duration_ms, notes))


def _to_num(v):
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


# ── Main sync logic ───────────────────────────────────────────────────────────

def run_sync(trigger: str = "daily_timer", day_offset: int = 0) -> dict:
    t0 = time.monotonic()
    logger.info("Scout sync starting — trigger=%s", trigger)

    events = fetch_ended_events(day_offset)
    fetched = len(events)
    logger.info("Fetched %d ended events", fetched)

    if not events:
        return {"fetched": 0, "upserted": 0, "errors": 0, "notes": "No events found"}

    upserted = errors = 0
    notes_list: list[str] = []

    with get_conn() as conn:
        with conn.cursor() as cur:
            for event in events:
                event_id = int(event["id"])
                try:
                    # Teams
                    for side in ("home", "away"):
                        team = event.get(side, {})
                        if team.get("id"):
                            upsert_team(cur, team)

                    # Event detail
                    detail = fetch_event_detail(str(event_id))
                    upsert_event(cur, event, detail)

                    # Stats
                    stats = fetch_event_stats(str(event_id))
                    upsert_stats(cur, event_id, stats)

                    # Odds snapshot
                    odds = fetch_event_odds(str(event_id))
                    if odds:
                        upsert_odds(cur, event_id, odds)

                    upserted += 1
                except Exception as exc:
                    errors += 1
                    msg = f"event {event_id}: {exc}"
                    logger.error(msg)
                    notes_list.append(msg)

            duration_ms = int((time.monotonic() - t0) * 1000)
            log_sync_run(
                cur, trigger, fetched, upserted, errors, duration_ms,
                notes="; ".join(notes_list[:5]) if notes_list else None,
            )
        conn.commit()

    result = {
        "trigger": trigger,
        "fetched": fetched,
        "upserted": upserted,
        "errors": errors,
        "duration_ms": int((time.monotonic() - t0) * 1000),
    }
    logger.info("Sync complete: %s", result)
    return result


# ── Azure Function entrypoint (Timer trigger) ─────────────────────────────────

app = func.FunctionApp()


@app.timer_trigger(
    schedule="0 0 3 * * *",      # daily at 03:00 UTC
    arg_name="timer",
    run_on_startup=False,
    use_monitor=True,
)
def daily_sync(timer: func.TimerRequest) -> None:
    """Runs every day at 03:00 UTC — syncs yesterday's matches to Supabase."""
    logger.info("daily_sync triggered (past_due=%s)", timer.past_due)
    result = run_sync(trigger="daily_timer")
    logger.info("daily_sync result: %s", result)

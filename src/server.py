"""FastAPI server — serves the overlay, admin panel, and pushes score updates via WebSocket."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
import re
from typing import Callable

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from src.majsoul_client import MajsoulClient
from src.tracker import GameTracker

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

# Load configs
PROJECT_ROOT = Path(__file__).parent.parent
LEGACY_CONFIG_ENV_VAR = "MAHJONG_TRACKER_CONFIG"
PLAYER_CONFIG_ENV_VAR = "MAHJONG_TRACKER_PLAYER_CONFIG"
TEAM_CONFIG_ENV_VAR = "MAHJONG_TRACKER_TEAM_CONFIG"


def _resolve_config_path(env_var: str, default_name: str, *, legacy_fallback: bool = False) -> Path:
    raw_value = os.environ.get(env_var)
    if raw_value is None and legacy_fallback:
        raw_value = os.environ.get(LEGACY_CONFIG_ENV_VAR)
    raw_path = Path(raw_value or default_name)
    if not raw_path.is_absolute():
        raw_path = PROJECT_ROOT / raw_path
    return raw_path


def _load_config(path: Path) -> dict | None:
    if not path.exists():
        return None
    with open(path) as f:
        return yaml.safe_load(f)


def _configured_contest_id(tracker_config: dict | None, fallback: int = 0) -> int:
    if not tracker_config:
        return fallback

    raw_value = tracker_config.get("contest_id", 0)
    try:
        contest_id = int(raw_value or 0)
    except (TypeError, ValueError):
        return fallback

    return contest_id or fallback


PLAYER_CONFIG_PATH = _resolve_config_path(
    PLAYER_CONFIG_ENV_VAR,
    "config.yaml",
)
TEAM_CONFIG_PATH = _resolve_config_path(
    TEAM_CONFIG_ENV_VAR,
    "config.semifinals.yaml",
    legacy_fallback=True,
)

player_config = _load_config(PLAYER_CONFIG_PATH) or {}
team_config = _load_config(TEAM_CONFIG_PATH) or None
config = player_config or team_config or {}

app = FastAPI(title="Mahjong Soul Tournament Tracker")

# Mount overlay static files
overlay_dir = Path(__file__).parent.parent / "overlay"
app.mount("/overlay", StaticFiles(directory=str(overlay_dir), html=True), name="overlay")

# WebSocket clients
player_ws_clients: set[WebSocket] = set()
team_ws_clients: set[WebSocket] = set()

# Global state
player_tracker: GameTracker | None = None
team_tracker: GameTracker | None = None
client: MajsoulClient | None = None
connection_status: dict = {
    "connected": False,
    "error": None,
    "account": None,
    "observer_level": None,
    "observer_error": None,
    "tracking_started_at": None,
    "team_observer_level": None,
    "team_observer_error": None,
    "team_tracking_started_at": None,
}


def _get_player_tracker() -> GameTracker | None:
    return player_tracker


def _get_team_tracker() -> GameTracker | None:
    return team_tracker


def _configured_team_entries(tracker_config: dict | None) -> list[dict]:
    """Return team config when the new grouped format is in use."""
    if not tracker_config:
        return []
    teams = tracker_config.get("teams", [])
    if isinstance(teams, list):
        return teams
    return []


def _flatten_team_players(teams: list[dict]) -> list[dict]:
    """Flatten grouped team config into player entries for API/admin consumers."""
    flattened_players: list[dict] = []

    for team_index, team in enumerate(teams):
        team_name = team.get("name", f"Team {team_index + 1}")
        team_group = team.get("group")
        team_id = team.get("id", team_name)
        for roster_index, player in enumerate(team.get("players", [])):
            player_entry = dict(player)
            player_entry.setdefault("team_id", team_id)
            player_entry.setdefault("team_name", team_name)
            player_entry.setdefault("team_group", team_group)
            player_entry.setdefault("team_index", team_index)
            player_entry.setdefault("roster_index", roster_index)
            flattened_players.append(player_entry)

    return flattened_players


def _configured_player_entries(tracker_config: dict | None) -> list[dict]:
    """Return the roster in a flat player list, regardless of config style."""
    if not tracker_config:
        return []

    teams = _configured_team_entries(tracker_config)
    if teams:
        return _flatten_team_players(teams)

    players = tracker_config.get("team_players", [])
    if isinstance(players, list):
        return players
    return []


def _make_tracker(
    tracker_config: dict | None,
    majsoul_client: MajsoulClient | None,
) -> GameTracker | None:
    """Create a GameTracker for a specific config."""
    if not tracker_config:
        return None

    teams = _configured_team_entries(tracker_config)
    return GameTracker(
        client=majsoul_client,
        players=_configured_player_entries(tracker_config),
        games_count=tracker_config["games_count"],
        uma_values=tracker_config["uma"],
        starting_points=tracker_config.get("starting_points", 25000),
        oka=tracker_config.get("oka", 0),
        poll_interval=tracker_config.get("poll_interval", 10),
        teams=teams or None,
    )


def _format_config_number(value: float | int) -> str:
    """Format ints/floats cleanly for config.yaml."""
    if isinstance(value, int):
        return str(value)
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _replace_config_line(text: str, key: str, value: str) -> str:
    """Replace a top-level YAML scalar line while preserving comments elsewhere."""
    pattern = re.compile(rf"^({re.escape(key)}:\s*).*$", re.MULTILINE)
    updated, count = pattern.subn(lambda match: f"{match.group(1)}{value}", text, count=1)
    if count != 1:
        raise RuntimeError(f"Could not update {key} in config.yaml")
    return updated


def _persist_scoring_config(
    config_path: Path,
    uma_values: list[float],
    starting_points: int,
    oka: float,
) -> None:
    """Persist runtime scoring settings back into a config file."""
    text = config_path.read_text(encoding="utf-8")
    uma_text = "[" + ", ".join(_format_config_number(value) for value in uma_values) + "]"
    text = _replace_config_line(text, "starting_points", _format_config_number(starting_points))
    text = _replace_config_line(text, "uma", uma_text)
    text = _replace_config_line(text, "oka", _format_config_number(oka))
    config_path.write_text(text, encoding="utf-8")


def _tracker_snapshot(tracker: GameTracker | None) -> dict:
    if not tracker:
        return {
            "players": [],
            "teams": [],
            "has_teams": False,
            "games_count": 0,
            "uma": [],
            "observer_level": None,
            "observer_error": None,
        }
    return tracker.get_state().to_dict()


async def _broadcast_tracker_state(
    tracker: GameTracker | None,
    ws_clients: set[WebSocket],
) -> None:
    """Push tracker state to all connected WebSocket clients."""
    if not tracker:
        return
    state = _tracker_snapshot(tracker)
    payload = json.dumps(state)
    dead = set()
    for ws in ws_clients:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    ws_clients.difference_update(dead)


async def broadcast_player_state() -> None:
    await _broadcast_tracker_state(player_tracker, player_ws_clients)


async def broadcast_team_state() -> None:
    await _broadcast_tracker_state(team_tracker, team_ws_clients)


def _parse_tracking_started_at(body: dict) -> tuple[int | None, str | None]:
    """Validate an optional tracking cutoff timestamp from an API body."""
    raw_value = body.get("tracking_started_at")
    if raw_value in (None, ""):
        return None, None

    try:
        tracking_started_at = int(raw_value)
    except (TypeError, ValueError):
        return None, "Tracking start time must be a Unix timestamp in seconds"

    if tracking_started_at <= 0:
        return None, "Tracking start time must be a positive Unix timestamp"

    return tracking_started_at, None


def _stop_trackers() -> None:
    for active_tracker in (player_tracker, team_tracker):
        if active_tracker:
            active_tracker.stop()


def _refresh_connection_status(account: dict | None = None) -> dict:
    connection_status["connected"] = account is not None
    connection_status["error"] = None if account is not None else connection_status.get("error")
    connection_status["account"] = account

    if player_tracker:
        connection_status["observer_level"] = player_tracker.observer_level
        connection_status["observer_error"] = player_tracker.observer_error
        connection_status["tracking_started_at"] = player_tracker.tracking_started_at
    else:
        connection_status["observer_level"] = None
        connection_status["observer_error"] = None
        connection_status["tracking_started_at"] = None

    if team_tracker:
        connection_status["team_observer_level"] = team_tracker.observer_level
        connection_status["team_observer_error"] = team_tracker.observer_error
        connection_status["team_tracking_started_at"] = team_tracker.tracking_started_at
    else:
        connection_status["team_observer_level"] = None
        connection_status["team_observer_error"] = None
        connection_status["team_tracking_started_at"] = None

    return connection_status


def _initialize_demo_trackers() -> None:
    global player_tracker, team_tracker
    player_tracker = _make_tracker(player_config, None)
    team_tracker = _make_tracker(team_config, None)


async def _initialize_connected_trackers(
    *,
    tracking_started_at: int | None,
) -> None:
    global player_tracker, team_tracker

    player_tracker = _make_tracker(player_config, client)
    team_tracker = _make_tracker(team_config, client)

    if player_tracker:
        player_tracker.on_update(broadcast_player_state)
        contest_id = _configured_contest_id(player_config)
        if contest_id:
            await player_tracker.start(
                contest_id,
                tracking_started_at=tracking_started_at,
            )

    if team_tracker:
        team_tracker.on_update(broadcast_team_state)
        contest_id = _configured_contest_id(
            team_config,
            _configured_contest_id(player_config),
        )
        if contest_id:
            await team_tracker.start(
                contest_id,
                tracking_started_at=tracking_started_at,
            )


async def try_connect(
    yostar_uid: str = "",
    yostar_token: str = "",
    gateway_token: str = "",
    tracking_started_at: int | None = None,
) -> dict:
    """Attempt Majsoul connection + login.

    Supports two modes:
      1. Yostar login info (LOGIN_UID + LOGIN_TOKEN) → oauth2Auth → oauth2Login
      2. Gateway access_token directly → oauth2Login (skips oauth2Auth)
    """
    global client, connection_status

    _stop_trackers()

    # Close existing connection
    if client:
        try:
            await client.close()
        except Exception:
            pass

    try:
        client = MajsoulClient(server=config.get("server", "en"))
        await client.connect()

        if gateway_token:
            account = await client.login_with_gateway_token(gateway_token)
        elif yostar_uid and yostar_token:
            account = await client.login_with_yostar_token(yostar_uid, yostar_token)
        else:
            raise RuntimeError("No credentials provided")

        await _initialize_connected_trackers(
            tracking_started_at=tracking_started_at,
        )

        connection_status = _refresh_connection_status(account)
        logger.info(
            "Connected and tracking player contest %s / team contest %s",
            _configured_contest_id(player_config),
            _configured_contest_id(team_config, _configured_contest_id(player_config)),
        )
        return connection_status

    except Exception as e:
        logger.error("Connection failed: %s", e)
        client = None
        _initialize_demo_trackers()
        connection_status = {
            "connected": False,
            "error": str(e),
            "account": None,
            "observer_level": None,
            "observer_error": None,
            "tracking_started_at": None,
            "team_observer_level": None,
            "team_observer_error": None,
            "team_tracking_started_at": None,
        }
        return connection_status


# ─── WebSocket ────────────────────────────────────────────

async def _tracker_websocket(
    ws: WebSocket,
    *,
    tracker_getter: Callable[[], GameTracker | None],
    ws_clients: set[WebSocket],
    allow_admin_commands: bool,
) -> None:
    await ws.accept()
    ws_clients.add(ws)
    logger.info("Overlay client connected (%d total)", len(ws_clients))

    tracker = tracker_getter()
    if tracker:
        await ws.send_text(json.dumps(_tracker_snapshot(tracker)))

    try:
        while True:
            data = await ws.receive_text()
            try:
                msg = json.loads(data)
                if allow_admin_commands:
                    await handle_admin_command(msg, ws)
                elif msg.get("command") == "refresh":
                    tracker = tracker_getter()
                    if tracker:
                        await ws.send_text(json.dumps(_tracker_snapshot(tracker)))
            except json.JSONDecodeError:
                pass
    except WebSocketDisconnect:
        ws_clients.discard(ws)
        logger.info("Overlay client disconnected (%d remaining)", len(ws_clients))


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await _tracker_websocket(
        ws,
        tracker_getter=_get_player_tracker,
        ws_clients=player_ws_clients,
        allow_admin_commands=True,
    )


@app.websocket("/ws/team")
async def websocket_team_endpoint(ws: WebSocket):
    await _tracker_websocket(
        ws,
        tracker_getter=_get_team_tracker,
        ws_clients=team_ws_clients,
        allow_admin_commands=False,
    )


async def handle_admin_command(msg: dict, ws: WebSocket) -> None:
    """Handle admin commands from the overlay/admin panel."""
    if not player_tracker:
        return

    cmd = msg.get("command")
    if cmd == "add_result":
        success = player_tracker.manual_add_result(
            player_name=msg["player"],
            placement=msg["placement"],
            raw_score=msg["score"],
        )
        if success:
            await broadcast_player_state()
        await ws.send_text(json.dumps({"ack": cmd, "success": success}))

    elif cmd == "reset_player":
        success = player_tracker.manual_reset_player(player_name=msg["player"])
        if success:
            await broadcast_player_state()
        await ws.send_text(json.dumps({"ack": cmd, "success": success}))

    elif cmd == "refresh":
        await broadcast_player_state()


# ─── API ──────────────────────────────────────────────────

@app.get("/api/scores")
async def get_scores():
    if not player_tracker:
        return JSONResponse(
            {"error": "Tracker not initialized"},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )
    return JSONResponse(
        _tracker_snapshot(player_tracker),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/api/scores/team")
async def get_team_scores():
    if not team_tracker:
        return JSONResponse(
            {"error": "Team tracker not initialized"},
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
        )
    return JSONResponse(
        _tracker_snapshot(team_tracker),
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
    )


@app.get("/api/config")
async def get_config():
    teams = _configured_team_entries(player_config)
    players = _configured_player_entries(player_config)
    return {
        "mode": "teams" if teams else "players",
        "players": [p.get("name", str(p["account_id"])) for p in players],
        "teams": [
            {
                "name": team.get("name", f"Team {index + 1}"),
                "group": team.get("group"),
                "players": [
                    player.get("name", str(player["account_id"]))
                    for player in team.get("players", [])
                ],
            }
            for index, team in enumerate(teams)
        ],
        "games_count": player_config["games_count"],
        "uma": player_config["uma"],
        "starting_points": player_config.get("starting_points", 25000),
        "oka": player_config.get("oka", 0),
    }


@app.get("/api/status")
async def get_status():
    if connection_status.get("connected"):
        _refresh_connection_status(connection_status.get("account"))
    return connection_status


@app.post("/api/connect")
async def api_connect(request: Request):
    """Runtime connect/reconnect with credentials."""
    body = await request.json()
    mode = body.get("mode", "yostar")
    tracking_started_at, error = _parse_tracking_started_at(body)
    logger.info(
        "Connect request: mode=%s, tracking_started_at=%s (raw=%r)",
        mode, tracking_started_at, body.get("tracking_started_at"),
    )
    if error:
        return JSONResponse({"error": error}, status_code=400)

    if mode == "gateway":
        gateway_token = body.get("gateway_token", "").strip()
        if not gateway_token:
            return JSONResponse({"error": "Gateway access_token is required"}, status_code=400)
        result = await try_connect(
            gateway_token=gateway_token,
            tracking_started_at=tracking_started_at,
        )
    else:
        uid = body.get("uid", "").strip()
        token = body.get("token", "").strip()
        if not uid or not token:
            return JSONResponse(
                {"error": "Both Login UID and Login Token are required"},
                status_code=400,
            )
        result = await try_connect(
            yostar_uid=uid,
            yostar_token=token,
            tracking_started_at=tracking_started_at,
        )

    return result


@app.post("/api/scoring")
async def api_update_scoring(request: Request):
    """Update runtime scoring settings and recompute finished game totals."""
    body = await request.json()

    raw_uma = body.get("uma")
    if not isinstance(raw_uma, list) or len(raw_uma) != 4:
        return JSONResponse({"error": "UMA must contain exactly 4 values"}, status_code=400)

    try:
        uma_values = [float(value) for value in raw_uma]
    except (TypeError, ValueError):
        return JSONResponse({"error": "UMA values must be numeric"}, status_code=400)

    try:
        starting_points = int(body.get("starting_points", player_config.get("starting_points", 25000)))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Starting points must be an integer"}, status_code=400)

    try:
        oka = float(body.get("oka", player_config.get("oka", 0)))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Oka must be numeric"}, status_code=400)

    if starting_points <= 0:
        return JSONResponse({"error": "Starting points must be positive"}, status_code=400)

    player_config["uma"] = uma_values
    player_config["starting_points"] = starting_points
    player_config["oka"] = oka
    if team_config is not None:
        team_config["uma"] = list(uma_values)
        team_config["starting_points"] = starting_points
        team_config["oka"] = oka

    try:
        updated_paths: list[Path] = []
        for config_path in (PLAYER_CONFIG_PATH, TEAM_CONFIG_PATH):
            if config_path in updated_paths or not config_path.exists():
                continue
            _persist_scoring_config(config_path, uma_values, starting_points, oka)
            updated_paths.append(config_path)
    except Exception as e:
        logger.error("Failed to persist scoring config: %s", e)
        return JSONResponse({"error": f"Could not save config: {e}"}, status_code=500)

    if player_tracker:
        player_tracker.update_scoring(
            uma_values=uma_values,
            starting_points=starting_points,
            oka=oka,
        )
        await broadcast_player_state()
    if team_tracker:
        team_tracker.update_scoring(
            uma_values=uma_values,
            starting_points=starting_points,
            oka=oka,
        )
        await broadcast_team_state()

    logger.info(
        "Scoring updated via admin: uma=%s starting_points=%s oka=%s",
        uma_values,
        starting_points,
        oka,
    )
    return {
        "success": True,
        "message": "Scoring updated",
        "scoring": {
            "uma": uma_values,
            "starting_points": starting_points,
            "oka": oka,
        },
    }


@app.post("/api/send_code")
async def api_send_code(request: Request):
    """Send email verification code."""
    body = await request.json()
    email = body.get("email", "").strip()
    if not email:
        return JSONResponse({"error": "Email required"}, status_code=400)

    # Ensure we have a connection to get game config
    global client
    if not client or not client._connected:
        try:
            client = MajsoulClient(server=config.get("server", "en"))
            await client.connect()
        except Exception as e:
            return JSONResponse({"error": f"Cannot connect to server: {e}"}, status_code=500)

    try:
        await client.send_email_code(email)
        return {"success": True, "message": f"Code sent to {email}"}
    except Exception as e:
        logger.error("Send code failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/verify_code")
async def api_verify_code(request: Request):
    """Verify email code and login."""
    global client, connection_status

    body = await request.json()
    email = body.get("email", "").strip()
    code = body.get("code", "").strip()
    tracking_started_at, error = _parse_tracking_started_at(body)
    if not email or not code:
        return JSONResponse({"error": "Email and code required"}, status_code=400)
    if error:
        return JSONResponse({"error": error}, status_code=400)

    if not client or not client._connected:
        return JSONResponse({"error": "Not connected to server"}, status_code=500)

    _stop_trackers()

    try:
        account = await client.login_with_email_code(email, code)

        await _initialize_connected_trackers(
            tracking_started_at=tracking_started_at,
        )

        connection_status = _refresh_connection_status(account)
        logger.info("Connected via email login: %s", account)
        return connection_status

    except Exception as e:
        logger.error("Email login failed: %s", e)
        connection_status = {
            "connected": False,
            "error": str(e),
            "account": None,
            "observer_level": None,
            "observer_error": None,
            "tracking_started_at": None,
            "team_observer_level": None,
            "team_observer_error": None,
            "team_tracking_started_at": None,
        }
        return JSONResponse({"error": str(e)}, status_code=500)


# ─── Admin Panel ──────────────────────────────────────────

@app.get("/admin", response_class=HTMLResponse)
async def admin_page():
    uid = config.get("yostar_uid", "")
    return ADMIN_HTML.replace("__DEFAULT_UID__", str(uid))


ADMIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Tracker Admin</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { font-family: system-ui, sans-serif; background: #0e0e14; color: #e0e0e0; padding: 24px; }
  h1 { font-size: 20px; margin-bottom: 16px; color: #fff; }
  .card { background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
          border-radius: 10px; padding: 20px; margin-bottom: 16px; max-width: 520px; }
  .card h2 { font-size: 14px; color: #888; text-transform: uppercase; letter-spacing: 0.5px;
             margin-bottom: 12px; }
  label { display: block; font-size: 12px; color: #999; margin-bottom: 4px; margin-top: 10px; }
  input, textarea { width: 100%; padding: 8px 10px; background: rgba(255,255,255,0.06);
          border: 1px solid rgba(255,255,255,0.1); border-radius: 6px; color: #fff;
          font-family: monospace; font-size: 13px; outline: none; resize: none; }
  input:focus, textarea:focus { border-color: #4ade80; }
  button { margin-top: 10px; padding: 8px 20px; background: #4ade80; color: #000; font-weight: 600;
           border: none; border-radius: 6px; cursor: pointer; font-size: 13px; }
  button:hover { background: #22c55e; }
  button:disabled { background: #333; color: #666; cursor: not-allowed; }
  .banner { font-size: 13px; padding: 10px 14px; border-radius: 8px; margin-bottom: 14px; }
  .banner.ok { background: rgba(74,222,128,0.1); border: 1px solid rgba(74,222,128,0.2); color: #4ade80; }
  .banner.err { background: rgba(248,113,113,0.08); border: 1px solid rgba(248,113,113,0.2); color: #f87171; }
  .banner.pending { background: rgba(250,204,21,0.08); border: 1px solid rgba(250,204,21,0.2); color: #facc15; }
  .banner .err-detail { font-size: 11px; color: #999; margin-top: 6px; word-break: break-all; }
  .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px;
         vertical-align: middle; }
  .dot.green { background: #4ade80; }
  .dot.red { background: #f87171; }
  .dot.grey { background: #555; }
  .divider { border-top: 1px solid rgba(255,255,255,0.06); margin: 14px 0; }
  .tabs { display: flex; gap: 0; margin-bottom: 16px; }
  .tab { padding: 8px 16px; font-size: 13px; cursor: pointer; border: 1px solid rgba(255,255,255,0.08);
         background: transparent; color: #888; }
  .tab:first-child { border-radius: 6px 0 0 6px; }
  .tab:last-child { border-radius: 0 6px 6px 0; }
  .tab.active { background: rgba(74,222,128,0.1); color: #4ade80; border-color: rgba(74,222,128,0.3); }
  .tab-content { display: none; }
  .tab-content.active { display: block; }
  .connected-info { padding: 12px 14px; background: rgba(74,222,128,0.06); border-radius: 8px;
                    border: 1px solid rgba(74,222,128,0.15); }
  .connected-info strong { color: #4ade80; }
  .connected-info .sub { font-size: 11px; color: #888; margin-top: 4px; }
  .btn-disconnect { margin-top: 10px; padding: 6px 14px; background: transparent;
                    border: 1px solid rgba(248,113,113,0.3); color: #f87171; font-size: 12px;
                    border-radius: 6px; cursor: pointer; }
  .btn-disconnect:hover { background: rgba(248,113,113,0.1); }
  .btn-secondary { background: rgba(255,255,255,0.08); color: #ccc; border: 1px solid rgba(255,255,255,0.1); }
  .btn-secondary:hover { background: rgba(255,255,255,0.12); color: #fff; }
  .hidden { display: none; }
  .help { font-size: 11px; color: #555; margin-top: 8px; line-height: 1.5; }
  .scoring-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
  .formula { font-size: 12px; color: #b8b8b8; line-height: 1.6; margin-bottom: 8px; }
  @media (max-width: 640px) {
    .scoring-grid { grid-template-columns: 1fr; }
  }
</style>
</head>
<body>
<h1>Mahjong Soul Tracker</h1>

<div id="status-banner" class="hidden"></div>

<div class="card">
  <h2>Connection</h2>

  <!-- Connected state -->
  <div id="connected-view" class="hidden">
    <div class="connected-info">
      <span class="dot green"></span> Connected as <strong id="nick"></strong>
      <div class="sub">Account ID: <span id="aid"></span></div>
      <div class="sub">Tracking from: <span id="tracking-start-display"></span></div>
      <div class="sub">Contest observer info: <span id="observer-status"></span></div>
      <div id="observer-detail" class="sub hidden" style="color:#f59e0b"></div>
    </div>
    <button class="btn-disconnect" onclick="showSetup()">Disconnect &amp; Reconfigure</button>
  </div>

  <!-- Setup form -->
  <div id="setup-form">
    <div class="tabs">
      <div class="tab active" onclick="switchTab('email')">Email Login</div>
      <div class="tab" onclick="switchTab('token')">Yostar Token</div>
      <div class="tab" onclick="switchTab('gateway')">Gateway Token</div>
    </div>

    <label for="tracking_started_at">Track Results From</label>
    <input type="datetime-local" id="tracking_started_at">
    <p class="help" style="color:#bbb; margin-bottom: 12px;">
      Only import tournament-log results that ended at or after this time. Defaults to now, so
      you can reconnect later with the original tracking start time if needed.
    </p>

    <!-- Email login tab -->
    <div id="tab-email" class="tab-content active">
      <p class="help" style="color:#bbb; margin-bottom: 8px;">
        Login with your Mahjong Soul account email. A verification code will be sent to you.
      </p>

      <div id="email-step1">
        <label for="email">Email Address</label>
        <input type="email" id="email" placeholder="your.email@example.com">
        <button id="btn-send-code" onclick="sendCode()">Send Verification Code</button>
      </div>

      <div id="email-step2" class="hidden">
        <div class="banner ok" style="margin-bottom:10px">
          Code sent to <strong id="email-sent-to"></strong>
        </div>
        <label for="code">Verification Code</label>
        <input type="text" id="code" placeholder="Enter 6-digit code from email" maxlength="10">
        <button id="btn-verify" onclick="verifyCode()">Verify &amp; Connect</button>
        <button class="btn-secondary" onclick="resetEmail()" style="margin-left: 8px;">Back</button>
      </div>

      <div id="email-result"></div>
    </div>

    <!-- Yostar Token tab -->
    <div id="tab-token" class="tab-content">
      <p class="help" style="color:#bbb; margin-bottom: 8px;">
        1. Open <a href="https://mahjongsoul.game.yo-star.com" target="_blank" style="color:#4ade80">Mahjong Soul</a> in your browser &amp; log in<br>
        2. Press F12 → Console tab<br>
        3. Run <code style="color:#4ade80">GameMgr.Inst.yostar_login_info</code><br>
        4. Copy <code style="color:#4ade80">LOGIN_UID</code> and <code style="color:#4ade80">LOGIN_TOKEN</code><br>
        <em style="color:#666">Use these values, not GameMgr.Inst.yostar_accessToken.</em>
      </p>
      <label for="yostar_uid">Login UID <span style="color:#666">(GameMgr.Inst.yostar_login_info.LOGIN_UID)</span></label>
      <input type="text" id="yostar_uid" placeholder="e.g. 14929394196">
      <label for="yostar_token">Login Token <span style="color:#666">(GameMgr.Inst.yostar_login_info.LOGIN_TOKEN)</span></label>
      <input type="text" id="yostar_token" placeholder="e.g. ac0f9c25434a852382dc...">
      <button id="btn-token" onclick="doConnect()">Connect</button>
      <div id="token-result"></div>
    </div>

    <!-- Gateway Token tab -->
    <div id="tab-gateway" class="tab-content">
      <p class="help" style="color:#bbb; margin-bottom: 8px;">
        Login using a gateway access_token captured from the game client.<br>
        In browser DevTools (F12) → Network tab → WS filter:<br>
        1. Find the WebSocket connection to the gateway<br>
        2. Look for an <code style="color:#4ade80">oauth2Login</code> or <code style="color:#4ade80">login</code> response<br>
        3. Copy the <code style="color:#4ade80">access_token</code> from the response<br>
        <em style="color:#666">Note: Email Login tab is simpler and recommended.</em>
      </p>
      <label for="gateway_token">Gateway Access Token</label>
      <input type="text" id="gateway_token" placeholder="Paste gateway access_token here">
      <button id="btn-gateway" onclick="doGatewayConnect()">Connect</button>
      <div id="gateway-result"></div>
    </div>
  </div>
</div>

<div class="card">
  <h2>Scoring</h2>
  <p class="formula">
    Adjusted score = (raw score - starting points) / 1000 + placement UMA + oka for 1st place.
    Saving here recalculates every tracked finished game immediately.
  </p>
  <div class="scoring-grid">
    <div>
      <label for="uma1">1st Place UMA</label>
      <input type="number" id="uma1" step="0.1" placeholder="15">
    </div>
    <div>
      <label for="uma2">2nd Place UMA</label>
      <input type="number" id="uma2" step="0.1" placeholder="5">
    </div>
    <div>
      <label for="uma3">3rd Place UMA</label>
      <input type="number" id="uma3" step="0.1" placeholder="-5">
    </div>
    <div>
      <label for="uma4">4th Place UMA</label>
      <input type="number" id="uma4" step="0.1" placeholder="-15">
    </div>
    <div>
      <label for="starting_points">Starting Points</label>
      <input type="number" id="starting_points" step="100" placeholder="25000">
    </div>
    <div>
      <label for="oka">Oka</label>
      <input type="number" id="oka" step="0.1" placeholder="0">
    </div>
  </div>
  <button id="btn-save-scoring" onclick="saveScoring()">Save Scoring</button>
  <div id="scoring-result"></div>
</div>

<div class="card">
  <h2>Overlay</h2>
  <p style="font-size:13px">
    Player overlay: <code>http://localhost:8765/overlay/</code> (uses <code>config.yaml</code>)<br>
    Team overlay: <code>http://localhost:8765/overlay/team.html</code> (uses <code>config.semifinals.yaml</code>)<br>
    Team preview: <code>http://localhost:8765/overlay/team_preview.html</code><br>
    Alternating overlay: <code>http://localhost:8765/overlay/cycle.html</code>
  </p>
  <p class="help" style="color:#bbb">
    Both trackers are loaded in parallel by default. The overlay now self-syncs every 10 seconds. If you want OBS to force a full page reload on a timer too,
    use <code>http://localhost:8765/overlay/?reload=300</code> for every 5 minutes.
  </p>
</div>

<script>
let currentEmail = '';

function pad2(value) {
  return String(value).padStart(2, '0');
}

function formatTrackingInputValue(date) {
  return date.getFullYear() + '-' +
    pad2(date.getMonth() + 1) + '-' +
    pad2(date.getDate()) + 'T' +
    pad2(date.getHours()) + ':' +
    pad2(date.getMinutes());
}

function initializeTrackingStartedAtInput() {
  const input = document.getElementById('tracking_started_at');
  if (!input || input.value) {
    return;
  }
  const now = new Date();
  now.setSeconds(0, 0);
  input.value = formatTrackingInputValue(now);
}

function setTrackingStartedAtInput(unixSeconds) {
  const input = document.getElementById('tracking_started_at');
  if (!input) {
    return;
  }
  if (typeof unixSeconds === 'number' && Number.isFinite(unixSeconds) && unixSeconds > 0) {
    input.value = formatTrackingInputValue(new Date(unixSeconds * 1000));
    return;
  }
  initializeTrackingStartedAtInput();
}

function getTrackingStartedAtPayload() {
  const input = document.getElementById('tracking_started_at');
  if (!input) {
    return {tracking_started_at: null};
  }

  const value = input.value.trim();
  if (!value) {
    return {tracking_started_at: null};
  }

  const match = value.match(/^(\\d{4})-(\\d{2})-(\\d{2})T(\\d{2}):(\\d{2})$/);
  if (!match) {
    return {error: 'Enter a valid tracking start time'};
  }

  const date = new Date(
    Number(match[1]),
    Number(match[2]) - 1,
    Number(match[3]),
    Number(match[4]),
    Number(match[5]),
    0,
    0
  );
  const unixMillis = date.getTime();
  if (Number.isNaN(unixMillis)) {
    return {error: 'Enter a valid tracking start time'};
  }

  return {tracking_started_at: Math.floor(unixMillis / 1000)};
}

function formatTrackingStartedAtLabel(unixSeconds) {
  if (typeof unixSeconds !== 'number' || !Number.isFinite(unixSeconds) || unixSeconds <= 0) {
    return 'Now';
  }
  return new Date(unixSeconds * 1000).toLocaleString();
}

function formatSettingNumber(value) {
  if (Number.isInteger(value)) {
    return String(value);
  }
  return String(value);
}

function setScoringForm(cfg) {
  const uma = cfg?.uma || [];
  document.getElementById('uma1').value = formatSettingNumber(uma[0] ?? 15);
  document.getElementById('uma2').value = formatSettingNumber(uma[1] ?? 5);
  document.getElementById('uma3').value = formatSettingNumber(uma[2] ?? -5);
  document.getElementById('uma4').value = formatSettingNumber(uma[3] ?? -15);
  document.getElementById('starting_points').value = formatSettingNumber(cfg?.starting_points ?? 25000);
  document.getElementById('oka').value = formatSettingNumber(cfg?.oka ?? 0);
}

async function loadConfig() {
  try {
    const r = await fetch('/api/config');
    const s = await r.json();
    setScoringForm(s);
  } catch (e) {
    document.getElementById('scoring-result').innerHTML =
      '<div class="banner err" style="margin-top:10px">Could not load scoring config: ' + e.message + '</div>';
  }
}

function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
  document.getElementById('tab-' + tab).classList.add('active');
  document.querySelector('.tab[onclick*="' + tab + '"]').classList.add('active');
}

function observerSummary(status) {
  const level = status?.observer_level;
  if (level == null) return 'unknown';
  return 'reported level ' + level;
}

function showConnected(status) {
  userRequestedSetup = false;
  const account = status.account || {};
  document.getElementById('nick').textContent = account.nickname || 'Unknown';
  document.getElementById('aid').textContent = account.account_id || '?';
  document.getElementById('tracking-start-display').textContent =
    formatTrackingStartedAtLabel(status.tracking_started_at);
  document.getElementById('observer-status').textContent = observerSummary(status);
  setTrackingStartedAtInput(status.tracking_started_at);
  const detail = document.getElementById('observer-detail');
  if (status.observer_error) {
    detail.textContent = status.observer_error;
    detail.classList.remove('hidden');
  } else {
    detail.textContent = '';
    detail.classList.add('hidden');
  }
  document.getElementById('connected-view').classList.remove('hidden');
  document.getElementById('setup-form').classList.add('hidden');
  document.getElementById('status-banner').className = 'hidden';
}

function showSetup(errorMsg) {
  userRequestedSetup = true;
  document.getElementById('connected-view').classList.add('hidden');
  document.getElementById('setup-form').classList.remove('hidden');
  initializeTrackingStartedAtInput();
  resetEmail();

  const banner = document.getElementById('status-banner');
  if (errorMsg) {
    banner.className = 'banner err';
    banner.innerHTML = '<span class="dot red"></span> Connection failed' +
      '<div class="err-detail">' + errorMsg + '</div>';
  } else {
    banner.className = 'hidden';
  }

  document.getElementById('email-result').innerHTML = '';
  document.getElementById('token-result').innerHTML = '';
}

function resetEmail() {
  document.getElementById('email-step1').classList.remove('hidden');
  document.getElementById('email-step2').classList.add('hidden');
  document.getElementById('email-result').innerHTML = '';
  document.getElementById('btn-send-code').disabled = false;
  document.getElementById('btn-send-code').textContent = 'Send Verification Code';
}

let userRequestedSetup = false;

async function checkStatus() {
  try {
    const r = await fetch('/api/status');
    const s = await r.json();
    if (s.connected && !userRequestedSetup) {
      showConnected(s);
    } else if (!document.getElementById('setup-form').classList.contains('hidden')) {
      // Only update banner if we're on the setup screen, don't interrupt user input
      if (s.error && !document.getElementById('email-step2').classList.contains('hidden') === false) {
        const banner = document.getElementById('status-banner');
        if (banner.className === 'hidden') {
          banner.className = 'banner err';
          banner.innerHTML = '<span class="dot red"></span> ' + s.error;
        }
      }
    }
  } catch(e) {}
}

async function sendCode() {
  const email = document.getElementById('email').value.trim();
  const btn = document.getElementById('btn-send-code');
  const result = document.getElementById('email-result');

  if (!email || !email.includes('@')) {
    result.innerHTML = '<div class="banner err" style="margin-top:10px">Enter a valid email</div>';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Sending...';
  result.innerHTML = '<div class="banner pending" style="margin-top:10px">Sending code...</div>';

  try {
    const r = await fetch('/api/send_code', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({email})
    });
    const s = await r.json();
    if (s.success) {
      currentEmail = email;
      document.getElementById('email-sent-to').textContent = email;
      document.getElementById('email-step1').classList.add('hidden');
      document.getElementById('email-step2').classList.remove('hidden');
      result.innerHTML = '';
    } else {
      result.innerHTML = '<div class="banner err" style="margin-top:10px">' + (s.error || 'Failed') + '</div>';
      btn.disabled = false;
      btn.textContent = 'Send Verification Code';
    }
  } catch(e) {
    result.innerHTML = '<div class="banner err" style="margin-top:10px">Request failed: ' + e.message + '</div>';
    btn.disabled = false;
    btn.textContent = 'Send Verification Code';
  }
}

async function verifyCode() {
  const code = document.getElementById('code').value.trim();
  const btn = document.getElementById('btn-verify');
  const result = document.getElementById('email-result');
  const tracking = getTrackingStartedAtPayload();

  if (!code) {
    result.innerHTML = '<div class="banner err" style="margin-top:10px">Enter the verification code</div>';
    return;
  }
  if (tracking.error) {
    result.innerHTML = '<div class="banner err" style="margin-top:10px">' + tracking.error + '</div>';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Verifying...';
  result.innerHTML = '<div class="banner pending" style="margin-top:10px">Logging in...</div>';

  try {
    const r = await fetch('/api/verify_code', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        email: currentEmail,
        code,
        tracking_started_at: tracking.tracking_started_at
      })
    });
    const s = await r.json();
    if (s.connected) {
      showConnected(s);
    } else {
      result.innerHTML = '<div class="banner err" style="margin-top:10px">' + (s.error || 'Failed') + '</div>';
    }
  } catch(e) {
    result.innerHTML = '<div class="banner err" style="margin-top:10px">' + e.message + '</div>';
  }
  btn.disabled = false;
  btn.textContent = 'Verify & Connect';
}

async function doConnect() {
  const uid = document.getElementById('yostar_uid').value.trim();
  const token = document.getElementById('yostar_token').value.trim();
  const btn = document.getElementById('btn-token');
  const result = document.getElementById('token-result');
  const tracking = getTrackingStartedAtPayload();

  if (!uid || !token) {
    result.innerHTML = '<div class="banner err" style="margin-top:10px">Both Login UID and Login Token are required</div>';
    return;
  }
  if (tracking.error) {
    result.innerHTML = '<div class="banner err" style="margin-top:10px">' + tracking.error + '</div>';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Connecting...';
  result.innerHTML = '<div class="banner pending" style="margin-top:10px">Connecting to Majsoul...</div>';
  document.getElementById('status-banner').className = 'hidden';

  try {
    const r = await fetch('/api/connect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        uid,
        token,
        tracking_started_at: tracking.tracking_started_at
      })
    });
    const s = await r.json();
    if (s.connected) {
      showConnected(s);
    } else {
      result.innerHTML = '<div class="banner err" style="margin-top:10px">' + (s.error || 'Unknown error') + '</div>';
    }
  } catch(e) {
    result.innerHTML = '<div class="banner err" style="margin-top:10px">' + e.message + '</div>';
  }
  btn.disabled = false;
  btn.textContent = 'Connect';
}

async function doGatewayConnect() {
  const token = document.getElementById('gateway_token').value.trim();
  const btn = document.getElementById('btn-gateway');
  const result = document.getElementById('gateway-result');
  const tracking = getTrackingStartedAtPayload();

  if (!token) {
    result.innerHTML = '<div class="banner err" style="margin-top:10px">Gateway token is required</div>';
    return;
  }
  if (tracking.error) {
    result.innerHTML = '<div class="banner err" style="margin-top:10px">' + tracking.error + '</div>';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Connecting...';
  result.innerHTML = '<div class="banner pending" style="margin-top:10px">Connecting...</div>';

  try {
    const r = await fetch('/api/connect', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        mode: 'gateway',
        gateway_token: token,
        tracking_started_at: tracking.tracking_started_at
      })
    });
    const s = await r.json();
    if (s.connected) {
      showConnected(s);
    } else {
      result.innerHTML = '<div class="banner err" style="margin-top:10px">' + (s.error || 'Unknown error') + '</div>';
    }
  } catch(e) {
    result.innerHTML = '<div class="banner err" style="margin-top:10px">' + e.message + '</div>';
  }
  btn.disabled = false;
  btn.textContent = 'Connect';
}

async function saveScoring() {
  const btn = document.getElementById('btn-save-scoring');
  const result = document.getElementById('scoring-result');
  const fields = ['uma1', 'uma2', 'uma3', 'uma4'];
  const uma = [];

  for (const field of fields) {
    const value = document.getElementById(field).value.trim();
    const parsed = Number(value);
    if (value === '' || !Number.isFinite(parsed)) {
      result.innerHTML = '<div class="banner err" style="margin-top:10px">Each UMA value must be numeric</div>';
      return;
    }
    uma.push(parsed);
  }

  const startingPointsValue = document.getElementById('starting_points').value.trim();
  const okaValue = document.getElementById('oka').value.trim();
  const startingPoints = Number(startingPointsValue);
  const oka = Number(okaValue);

  if (
    startingPointsValue === '' ||
    !Number.isInteger(startingPoints) ||
    startingPoints <= 0
  ) {
    result.innerHTML = '<div class="banner err" style="margin-top:10px">Starting points must be a positive integer</div>';
    return;
  }

  if (okaValue === '' || !Number.isFinite(oka)) {
    result.innerHTML = '<div class="banner err" style="margin-top:10px">Oka must be numeric</div>';
    return;
  }

  btn.disabled = true;
  btn.textContent = 'Saving...';
  result.innerHTML = '<div class="banner pending" style="margin-top:10px">Updating scoring and recomputing totals...</div>';

  try {
    const r = await fetch('/api/scoring', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        uma,
        starting_points: startingPoints,
        oka,
      })
    });
    const s = await r.json();
    if (!r.ok || !s.success) {
      result.innerHTML = '<div class="banner err" style="margin-top:10px">' + (s.error || 'Could not save scoring') + '</div>';
      return;
    }

    setScoringForm(s.scoring);
    result.innerHTML = '<div class="banner ok" style="margin-top:10px">Scoring updated. Finished game totals were recalculated.</div>';
  } catch (e) {
    result.innerHTML = '<div class="banner err" style="margin-top:10px">Request failed: ' + e.message + '</div>';
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save Scoring';
  }
}

initializeTrackingStartedAtInput();
loadConfig();
checkStatus();
setInterval(checkStatus, 15000);
</script>
</body>
</html>"""


# ─── Startup ──────────────────────────────────────────────

@app.on_event("startup")
async def startup():
    """Start in demo mode. User connects via admin panel."""
    global client
    _initialize_demo_trackers()

    # Pre-connect WebSocket so game config is loaded (needed for email login)
    try:
        client = MajsoulClient(server=config.get("server", "en"))
        await client.connect()
        logger.info("WebSocket connected. Use /admin to login.")
    except Exception as e:
        logger.warning("Pre-connect failed: %s. Will retry on login.", e)


def main():
    import uvicorn

    host = config.get("host", "0.0.0.0")
    port = config.get("port", 8765)
    logger.info("Starting server on %s:%d", host, port)
    logger.info("Player config: %s", PLAYER_CONFIG_PATH)
    logger.info("Team config: %s", TEAM_CONFIG_PATH if team_config else "disabled")
    logger.info("Admin panel: http://localhost:%d/admin", port)
    logger.info("Overlay URL: http://localhost:%d/overlay/", port)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()

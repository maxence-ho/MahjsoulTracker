"""Game tracker — polls Majsoul for contest game results and maintains tournament state."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import TYPE_CHECKING, Any, Callable

from src.models import (
    GameResult,
    PlayerState,
    PlayerStatus,
    TeamState,
    TournamentState,
)

if TYPE_CHECKING:
    from src.majsoul_client import GameObserver, MajsoulClient

logger = logging.getLogger(__name__)


class GameTracker:
    """Tracks tournament games, calculates UMA, and maintains player state."""

    def __init__(
        self,
        client: MajsoulClient,
        players: list[dict],
        games_count: int,
        uma_values: list[float],
        starting_points: int = 25000,
        oka: float = 0,
        poll_interval: int = 10,
        teams: list[dict] | None = None,
    ):
        self.client = client
        self.games_count = games_count
        self.uma_values = uma_values
        self.starting_points = starting_points
        self.oka = oka
        self.poll_interval = poll_interval

        # Init player states — use list to avoid duplicate account_id issues
        self.players_list: list[PlayerState] = []
        self.teams_list: list[TeamState] = []
        self._initialize_roster(players, teams or [])

        # Lookup by account_id (only works when IDs are configured)
        self.players: dict[int, PlayerState] = {
            p.account_id: p for p in self.players_list if p.account_id != 0
        }
        # Nickname-based lookup for fallback matching (lowercase)
        self._players_by_nickname: dict[str, PlayerState] = {
            p.name.lower(): p for p in self.players_list if p.name
        }

        self.contest_unique_id: int | None = None
        self._live_game_uuids: set[str] = set()
        self._seen_game_uuids: set[str] = set()
        self._deferred_game_results: dict[str, list[dict]] = {}
        self._pending_finished_game_uuids: set[str] = set()
        self._on_update: Callable[[], Any] | None = None
        self._running = False
        self._tracking_started_at: int | None = None
        self._poll_task: asyncio.Task[None] | None = None
        # Active game observers: game_uuid -> GameObserver
        self._observers: dict[str, GameObserver] = {}
        # Map game_uuid -> set of tracked account_ids in that game
        self._observed_players: dict[str, set[int]] = {}
        self.observer_level: int | None = None
        self.observer_error: str | None = None

    def on_update(self, callback: Callable[[], Any]) -> None:
        """Register callback to fire when state changes."""
        self._on_update = callback

    def get_state(self) -> TournamentState:
        """Get current tournament state."""
        return TournamentState(
            players=self.players_list,
            teams=self.teams_list,
            games_count=self.games_count,
            uma_values=self.uma_values,
            observer_level=self.observer_level,
            observer_error=self.observer_error,
        )

    def get_scoring_settings(self) -> dict[str, float | int | list[float]]:
        """Return the active scoring settings used for adjusted totals."""
        return {
            "uma": list(self.uma_values),
            "starting_points": self.starting_points,
            "oka": self.oka,
        }

    def _initialize_roster(self, players: list[dict], teams: list[dict]) -> None:
        """Build player and team state from either flat players or grouped teams."""
        if teams:
            self._initialize_team_roster(teams)
            return

        for player_index, player in enumerate(players):
            self.players_list.append(
                self._build_player_state(
                    player,
                    default_team_index=player_index,
                )
            )

        self.teams_list = self._build_teams_from_players()

    def _initialize_team_roster(self, teams: list[dict]) -> None:
        """Build player state from explicit team configuration."""
        group_positions: dict[str | None, int] = {}

        for team_index, team in enumerate(teams):
            team_name = team.get("name", f"Team {team_index + 1}")
            team_group = team.get("group")
            if team_group not in group_positions:
                group_positions[team_group] = len(group_positions)

            roster: list[PlayerState] = []
            for roster_index, player in enumerate(team.get("players", [])):
                player_state = self._build_player_state(
                    player,
                    team_id=self._team_identifier(team, team_index),
                    team_name=team_name,
                    team_group=team_group,
                    default_team_index=team_index,
                    roster_index=roster_index,
                )
                self.players_list.append(player_state)
                roster.append(player_state)

            self.teams_list.append(
                TeamState(
                    id=self._team_identifier(team, team_index),
                    name=team_name,
                    players=roster,
                    group=team_group,
                    group_index=group_positions[team_group],
                    sort_index=team_index,
                    games_per_player=self.games_count,
                )
            )

    def _build_player_state(
        self,
        player: dict,
        *,
        team_id: str | None = None,
        team_name: str | None = None,
        team_group: str | None = None,
        default_team_index: int = 0,
        roster_index: int = 0,
    ) -> PlayerState:
        """Normalize a config entry into a tracked player state."""
        account_id = int(player.get("account_id", 0))
        resolved_team_name = team_name or player.get("team_name")
        resolved_team_id = team_id or player.get("team_id")

        return PlayerState(
            name=player.get("name", str(account_id)),
            account_id=account_id,
            team_id=resolved_team_id,
            team_name=resolved_team_name,
            team_group=team_group or player.get("team_group"),
            team_index=int(player.get("team_index", default_team_index)),
            roster_index=int(player.get("roster_index", roster_index)),
        )

    def _build_teams_from_players(self) -> list[TeamState]:
        """Derive team rosters from player metadata when provided in flat config."""
        teams_by_id: dict[str, TeamState] = {}
        group_positions: dict[str | None, int] = {}

        for player in self.players_list:
            if not player.team_id and not player.team_name:
                continue

            team_id = player.team_id or _slugify_name(
                player.team_name or f"team-{player.team_index + 1}",
                fallback=f"team-{player.team_index + 1}",
            )
            team_group = player.team_group
            if team_group not in group_positions:
                group_positions[team_group] = len(group_positions)

            team_state = teams_by_id.get(team_id)
            if team_state is None:
                team_state = TeamState(
                    id=team_id,
                    name=player.team_name or f"Team {len(teams_by_id) + 1}",
                    group=team_group,
                    group_index=group_positions[team_group],
                    sort_index=player.team_index,
                    games_per_player=self.games_count,
                )
                teams_by_id[team_id] = team_state

            team_state.players.append(player)

        return sorted(teams_by_id.values(), key=lambda team: team.sort_index)

    def _team_identifier(self, team: dict, team_index: int) -> str:
        """Build a stable team identifier from config."""
        raw_value = team.get("id") or team.get("name") or f"team-{team_index + 1}"
        return _slugify_name(raw_value, fallback=f"team-{team_index + 1}")

    @property
    def tracking_started_at(self) -> int | None:
        """Unix timestamp cutoff for records counted in this tracking session."""
        return self._tracking_started_at

    def uma_for_placement(self, placement: int) -> float:
        """Return the configured UMA for a placement, defaulting to zero."""
        if 1 <= placement <= len(self.uma_values):
            return self.uma_values[placement - 1]
        return 0

    def calculate_adjusted_score(self, raw_score: int, placement: int) -> float:
        """Calculate adjusted score: (raw - start) / 1000 + uma [+ oka for 1st]."""
        base = (raw_score - self.starting_points) / 1000
        uma = self.uma_for_placement(placement)
        bonus = self.oka if placement == 1 else 0
        return round(base + uma + bonus, 1)

    def update_scoring(
        self,
        *,
        uma_values: list[float],
        starting_points: int | None = None,
        oka: float | None = None,
    ) -> None:
        """Update scoring settings and recompute all existing game results."""
        self.uma_values = list(uma_values)
        if starting_points is not None:
            self.starting_points = starting_points
        if oka is not None:
            self.oka = oka

        for player in self.players_list:
            for result in player.game_results:
                result.uma = self.uma_for_placement(result.placement)
                result.adjusted_score = self.calculate_adjusted_score(
                    result.raw_score,
                    result.placement,
                )

        logger.info(
            "Updated scoring settings: uma=%s starting_points=%s oka=%s",
            self.uma_values,
            self.starting_points,
            self.oka,
        )

    def _process_game_record(self, game_uuid: str, results: list[dict]) -> bool:
        """Process a completed game record. Returns True if any team player was in this game."""
        if game_uuid in self._seen_game_uuids:
            self._pending_finished_game_uuids.discard(game_uuid)
            return False

        self._seen_game_uuids.add(game_uuid)
        self._pending_finished_game_uuids.discard(game_uuid)
        found_team_player = False

        for result in results:
            account_id = result["account_id"]
            nickname = result.get("nickname", "").strip()

            player = None
            if account_id in self.players:
                player = self.players[account_id]
            elif nickname and nickname.lower() in self._players_by_nickname:
                player = self._players_by_nickname[nickname.lower()]
                old_aid = player.account_id
                logger.info(
                    "Nickname match in record: %s account_id %d → %d",
                    nickname, old_aid, account_id,
                )
                self.players.pop(old_aid, None)
                player.account_id = account_id
                self.players[account_id] = player

            if not player:
                continue

            found_team_player = True
            if nickname:
                player.nickname = nickname
            placement = result["placement"]
            raw_score = result["score"]
            adjusted = self.calculate_adjusted_score(raw_score, placement)

            game_result = GameResult(
                game_uuid=game_uuid,
                placement=placement,
                raw_score=raw_score,
                uma=self.uma_for_placement(placement),
                adjusted_score=adjusted,
            )
            player.game_results.append(game_result)
            if player.current_game_uuid in (None, game_uuid):
                player.status = (
                    PlayerStatus.FINISHED
                    if player.games_played >= self.games_count
                    else PlayerStatus.IDLE
                )
                player.current_game_uuid = None
                player.current_points = None
                player.current_round = None
                player.current_placement = None
                player.provisional_uma = None
                player.provisional_score = None

            logger.info(
                "%s finished game — %s place, %d pts, adjusted: %+.1f (total: %+.1f)",
                player.name,
                _ordinal(placement),
                raw_score,
                adjusted,
                player.total_score,
            )

        return found_team_player

    def _defer_game_record(self, game_uuid: str, results: list[dict], *, source: str) -> None:
        """Store final results until the game is no longer reported live."""
        if game_uuid in self._seen_game_uuids:
            return

        was_deferred = game_uuid in self._deferred_game_results
        self._deferred_game_results[game_uuid] = [dict(result) for result in results]
        if not was_deferred:
            logger.info(
                "Deferred final result for live game %s from %s until it leaves the live list",
                game_uuid,
                source,
            )

    def _apply_deferred_game_records(self, live_game_uuids: set[str]) -> bool:
        """Apply stored final results once the game is no longer live."""
        changed = False

        ready_game_uuids = [
            game_uuid
            for game_uuid in self._deferred_game_results
            if game_uuid not in live_game_uuids
        ]
        for game_uuid in ready_game_uuids:
            results = self._deferred_game_results.pop(game_uuid)
            if self._process_game_record(game_uuid, results):
                changed = True
            logger.info(
                "Applied deferred final result for %s after it left the live list",
                game_uuid,
            )

        return changed

    async def _poll_game_records(self) -> bool:
        """Poll tournament logs for finished games. Returns True if state changed."""
        if not self.contest_unique_id:
            return False

        try:
            records = await self.client.fetch_contest_game_records(self.contest_unique_id)
        except Exception as e:
            logger.error("Failed to fetch tournament logs: %s", e)
            return False

        changed = False

        for record in records:
            uuid = record["uuid"]
            if uuid in self._seen_game_uuids:
                continue

            if not self._should_track_record(record):
                self._seen_game_uuids.add(uuid)
                logger.info(
                    "Ignoring historical game %s from before tracking started",
                    uuid,
                )
                continue

            summary_results = record.get("players", [])
            if uuid in self._live_game_uuids:
                self._defer_game_record(uuid, summary_results, source="tournament logs")
                continue

            if self._process_game_record(uuid, summary_results):
                changed = True
                if uuid in self._pending_finished_game_uuids:
                    logger.info(
                        "Tournament logs reported final result for ended game %s",
                        uuid,
                    )

        return changed

    def _should_track_record(self, record: dict) -> bool:
        """Return whether a finished record should count for this tracking session."""
        if self._tracking_started_at is None:
            return True

        end_time = record.get("end_time")
        if not end_time:
            return True

        return end_time >= self._tracking_started_at

    async def _poll_live_games(self) -> bool:
        """Check for live games, start observers, update player statuses and live scores."""
        if not self.contest_unique_id:
            return False

        try:
            live_games = await self.client.fetch_live_games(self.contest_unique_id)
            if live_games:
                logger.info("Live games: %d found", len(live_games))
                for g in live_games:
                    logger.info("  Game %s: %s", g["uuid"], [(p["account_id"], p.get("nickname","")) for p in g["players"]])
        except Exception as e:
            logger.error("Failed to fetch live games: %s", e)
            return False

        # Track which players are currently in a live game
        changed = False
        live_account_ids: set[int] = set()
        live_game_uuids: set[str] = {game["uuid"] for game in live_games}
        self._live_game_uuids = set(live_game_uuids)

        for game_uuid, observer in list(self._observers.items()):
            if not observer.game_ended:
                continue

            final_results = observer.get_final_results()
            if final_results is not None:
                if game_uuid in live_game_uuids:
                    self._defer_game_record(game_uuid, final_results, source="game stream")
                    self._pending_finished_game_uuids.add(game_uuid)
                else:
                    if self._process_game_record(game_uuid, final_results):
                        changed = True
                    logger.info(
                        "Observed game %s ended; processed final result after it left the live list",
                        game_uuid,
                    )
                continue

            if game_uuid not in self._pending_finished_game_uuids:
                self._pending_finished_game_uuids.add(game_uuid)
                logger.info(
                    "Observed game %s ended; waiting for final record sync",
                    game_uuid,
                )

        if self._apply_deferred_game_records(live_game_uuids):
            changed = True

        for game in live_games:
            game_uuid = game["uuid"]
            if game_uuid in self._seen_game_uuids:
                continue

            tracked_in_game: set[int] = set()

            for player in game["players"]:
                aid = player["account_id"]
                nickname = player.get("nickname", "").strip()

                matched_player = None
                if aid in self.players:
                    matched_player = self.players[aid]
                elif nickname and nickname.lower() in self._players_by_nickname:
                    # Fallback: match by nickname when account_id changed
                    matched_player = self._players_by_nickname[nickname.lower()]
                    old_aid = matched_player.account_id
                    logger.info(
                        "Nickname match: %s account_id %d → %d",
                        nickname, old_aid, aid,
                    )
                    # Update account_id mappings
                    self.players.pop(old_aid, None)
                    matched_player.account_id = aid
                    self.players[aid] = matched_player

                if matched_player:
                    if nickname and matched_player.nickname != nickname:
                        matched_player.nickname = nickname
                    matched_player.current_game_uuid = game_uuid
                    live_account_ids.add(aid)
                    tracked_in_game.add(aid)

            # Start observer for new live games with tracked players,
            # or replace a dead observer whose connection dropped.
            if tracked_in_game:
                if game_uuid in self._pending_finished_game_uuids:
                    continue
                existing = self._observers.get(game_uuid)
                if existing and not existing.is_alive:
                    logger.info("Replacing dead observer for %s", game_uuid)
                    await self._stop_observer(game_uuid)
                if game_uuid not in self._observers:
                    await self._start_observer(game_uuid, tracked_in_game)

        # Close observers for games that are no longer live
        ended = [
            uuid
            for uuid in self._observers
            if uuid not in live_game_uuids
            or uuid in self._seen_game_uuids
            or uuid in self._pending_finished_game_uuids
        ]
        for uuid in ended:
            await self._stop_observer(uuid)

        # Update live scores and round from active observers
        for game_uuid, observer in self._observers.items():
            scores = observer.get_scores()
            placements = self._live_placements_from_scores(scores)
            current_round = observer.current_round
            for account_id, score in scores.items():
                if account_id in self.players:
                    player = self.players[account_id]
                    if player.current_points != score:
                        player.current_points = score
                        changed = True
                    if player.current_round != current_round:
                        player.current_round = current_round
                        changed = True
                    placement = placements.get(account_id)
                    if player.current_placement != placement:
                        player.current_placement = placement
                        changed = True
                    provisional_uma = (
                        self.uma_for_placement(placement)
                        if placement is not None
                        else None
                    )
                    if player.provisional_uma != provisional_uma:
                        player.provisional_uma = provisional_uma
                        changed = True
                    provisional_score = (
                        self.calculate_adjusted_score(score, placement)
                        if placement is not None
                        else None
                    )
                    if player.provisional_score != provisional_score:
                        player.provisional_score = provisional_score
                        changed = True

        # Update player statuses
        for player in self.players_list:
            if player.status == PlayerStatus.FINISHED:
                continue

            if player.account_id in live_account_ids:
                if player.status != PlayerStatus.IN_GAME:
                    player.status = PlayerStatus.IN_GAME
                    changed = True
                    logger.info("%s is now in a game", player.name)
            else:
                if player.status == PlayerStatus.IN_GAME:
                    player.status = PlayerStatus.IDLE
                    player.current_game_uuid = None
                    player.current_points = None
                    player.current_round = None
                    player.current_placement = None
                    player.provisional_uma = None
                    player.provisional_score = None
                    changed = True

        return changed

    async def _start_observer(self, game_uuid: str, account_ids: set[int]) -> None:
        """Start observing a live game to get real-time scores."""
        if not self.client:
            return
        try:
            observer = await self.client.observe_game(game_uuid)
            self._observers[game_uuid] = observer
            self._observed_players[game_uuid] = account_ids
            self.observer_error = None
            logger.info("Started observing game %s", game_uuid)

            # Immediately update scores from observer
            scores = observer.get_scores()
            placements = self._live_placements_from_scores(scores)
            for account_id, score in scores.items():
                if account_id in self.players:
                    player = self.players[account_id]
                    player.current_points = score
                    player.current_placement = placements.get(account_id)
                    player.provisional_uma = (
                        self.uma_for_placement(player.current_placement)
                        if player.current_placement is not None
                        else None
                    )
                    player.provisional_score = (
                        self.calculate_adjusted_score(score, player.current_placement)
                        if player.current_placement is not None
                        else None
                    )
        except Exception as e:
            self.observer_error = str(e)
            logger.warning("Could not observe game %s: %s", game_uuid, e)

    async def _stop_observer(self, game_uuid: str) -> None:
        """Stop observing a game."""
        observer = self._observers.pop(game_uuid, None)
        self._observed_players.pop(game_uuid, None)
        if observer:
            await observer.close()

    def _live_placements_from_scores(self, scores: dict[int, int]) -> dict[int, int]:
        """Estimate current placement from live point totals for a table."""
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return {
            account_id: placement
            for placement, (account_id, _score) in enumerate(ranked, start=1)
        }

    async def start(
        self,
        contest_id: int,
        tracking_started_at: int | None = None,
    ) -> None:
        """Initialize tracker: fetch contest info and start polling loop."""
        self._tracking_started_at = (
            tracking_started_at if tracking_started_at is not None else int(time.time())
        )

        # Resolve player nicknames from account IDs
        try:
            account_ids = sorted({p.account_id for p in self.players_list if p.account_id > 0})
            nicknames = await self.client.fetch_multi_account_brief(account_ids)
            for player in self.players_list:
                if player.account_id in nicknames:
                    player.nickname = nicknames[player.account_id]
                    logger.info("Resolved %s → %s", player.account_id, player.nickname)
        except Exception as e:
            logger.warning("Could not resolve player nicknames: %s", e)

        # Fetch contest info to get unique_id
        contest_info = await self.client.fetch_contest_info(contest_id)
        self.contest_unique_id = contest_info["unique_id"]
        logger.info(
            "Tracking contest: %s (unique_id=%s, started_at=%s)",
            contest_info["contest_name"],
            self.contest_unique_id,
            self._tracking_started_at,
        )

        # Enter contest lobby for potential notifications
        try:
            await self.client.enter_contest(self.contest_unique_id)
        except Exception as e:
            logger.warning("Could not enter contest lobby: %s (polling will still work)", e)

        try:
            auth_info = await self.client.fetch_contest_auth_info(self.contest_unique_id)
            self.observer_level = auth_info["observer_level"]
            self.observer_error = None
            logger.info("Contest observer level: %s", self.observer_level)
        except Exception as e:
            self.observer_level = None
            self.observer_error = f"Could not fetch contest observer info: {e}"
            logger.warning("Could not fetch contest observer info: %s", e)

        # Initial fetch of live tables plus only the records that finished
        # after this tracking session started.
        await self._poll_live_games()
        await self._poll_game_records()
        if self._on_update:
            await self._on_update()

        # Start polling loop
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        """Main polling loop."""
        logger.info("Polling for game updates every %ds...", self.poll_interval)
        try:
            while self._running:
                await asyncio.sleep(self.poll_interval)
                try:
                    live_changed = await self._poll_live_games()
                    records_changed = await self._poll_game_records()
                    if (records_changed or live_changed) and self._on_update:
                        await self._on_update()
                except Exception as e:
                    logger.error("Poll error: %s", e)
        except asyncio.CancelledError:
            return

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
        self._poll_task = None

    # --- Manual overrides for fallback ---

    def manual_add_result(
        self, player_name: str, placement: int, raw_score: int
    ) -> bool:
        """Manually add a game result (fallback if API misses it)."""
        player = self._find_player(player_name)
        if not player:
            return False

        adjusted = self.calculate_adjusted_score(raw_score, placement)
        result = GameResult(
            game_uuid=f"manual-{player.games_played + 1}",
            placement=placement,
            raw_score=raw_score,
            uma=self.uma_for_placement(placement),
            adjusted_score=adjusted,
        )
        player.game_results.append(result)
        player.status = (
            PlayerStatus.FINISHED
            if player.games_played >= self.games_count
            else PlayerStatus.IDLE
        )
        logger.info("Manually added result for %s", player.name)
        return True

    def manual_reset_player(self, player_name: str) -> bool:
        """Reset a player's results (undo mistakes)."""
        player = self._find_player(player_name)
        if not player:
            return False

        player.game_results.clear()
        player.status = PlayerStatus.IDLE
        player.current_game_uuid = None
        player.current_points = None
        player.current_round = None
        player.current_placement = None
        player.provisional_uma = None
        player.provisional_score = None
        logger.info("Reset results for %s", player.name)
        return True

    def _find_player(self, player_name: str) -> PlayerState | None:
        """Match a configured player by plain or qualified name."""
        normalized_name = player_name.strip().lower()
        for player in self.players_list:
            candidates = {
                player.name.lower(),
                player.display_name.lower(),
                player.qualified_name.lower(),
            }
            if player.team_name:
                candidates.add(f"{player.team_name} / {player.name}".lower())
            if normalized_name in candidates:
                return player
        return None


def _ordinal(n: int) -> str:
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(n, "th")
    return f"{n}{suffix}"


def _slugify_name(value: str, *, fallback: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return normalized or fallback

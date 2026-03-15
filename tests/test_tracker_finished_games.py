from __future__ import annotations

import asyncio
import unittest

from src.models import PlayerStatus
from src.tracker import GameTracker


class FakeObserver:
    def __init__(
        self,
        scores: dict[int, int] | None = None,
        *,
        game_ended: bool = False,
        final_results: list[dict] | None = None,
        current_round: str = "E1",
    ):
        self._scores = scores or {}
        self.game_ended = game_ended
        self._final_results = final_results
        self.current_round = current_round
        self.closed = False

    @property
    def is_alive(self) -> bool:
        return not self.closed

    def get_scores(self) -> dict[int, int]:
        return dict(self._scores)

    def get_final_results(self) -> list[dict] | None:
        if self._final_results is None:
            return None
        return [dict(result) for result in self._final_results]

    async def close(self) -> None:
        self.closed = True


class FakeClient:
    def __init__(
        self,
        *,
        live_games: list[dict] | None = None,
        game_records: list[dict] | None = None,
        contest_info: dict | None = None,
        auth_info: dict | None = None,
        observers: dict[str, FakeObserver] | None = None,
    ):
        self.live_games = live_games or []
        self.game_records = game_records or []
        self.contest_info = contest_info or {
            "unique_id": 999,
            "contest_name": "Test Contest",
        }
        self.auth_info = auth_info or {"observer_level": 0}
        self.observers = observers or {}
        self.entered_contests: list[int] = []

    async def fetch_live_games(self, unique_id: int) -> list[dict]:
        return list(self.live_games)

    async def fetch_contest_game_records(self, unique_id: int) -> list[dict]:
        return list(self.game_records)

    async def fetch_contest_info(self, contest_id: int) -> dict:
        return dict(self.contest_info)

    async def enter_contest(self, unique_id: int) -> None:
        self.entered_contests.append(unique_id)

    async def fetch_contest_auth_info(self, unique_id: int) -> dict:
        return dict(self.auth_info)

    async def observe_game(self, game_uuid: str) -> FakeObserver:
        observer = self.observers.get(game_uuid)
        if observer is None:
            raise RuntimeError("observer unavailable")
        return observer


class GameTrackerFinishedGamesTests(unittest.TestCase):
    def make_tracker(self, client=None) -> GameTracker:
        return GameTracker(
            client=client,
            players=[
                {"name": "Alpha", "account_id": 101},
                {"name": "Bravo", "account_id": 202},
            ],
            games_count=4,
            uma_values=[15, 5, -5, -15],
            starting_points=25000,
            oka=0,
        )

    def test_process_game_record_updates_placement_and_uma_for_single_followed_player(self) -> None:
        tracker = self.make_tracker()

        changed = tracker._process_game_record(
            "game-1",
            [
                {"account_id": 303, "nickname": "Other", "placement": 1, "score": 41000},
                {"account_id": 101, "nickname": "AlphaLive", "placement": 2, "score": 32000},
                {"account_id": 404, "nickname": "Other2", "placement": 3, "score": 18000},
                {"account_id": 505, "nickname": "Other3", "placement": 4, "score": 9000},
            ],
        )

        self.assertTrue(changed)

        alpha = tracker.players[101]
        self.assertEqual(alpha.nickname, "AlphaLive")
        self.assertEqual(alpha.status, PlayerStatus.IDLE)
        self.assertEqual(alpha.games_played, 1)
        self.assertEqual(len(alpha.game_results), 1)

        result = alpha.game_results[0]
        self.assertEqual(result.game_uuid, "game-1")
        self.assertEqual(result.placement, 2)
        self.assertEqual(result.raw_score, 32000)
        self.assertEqual(result.uma, 5)
        self.assertEqual(result.adjusted_score, 12.0)
        self.assertEqual(alpha.total_score, 12.0)

    def test_process_game_record_updates_each_followed_player_in_same_game(self) -> None:
        tracker = self.make_tracker()

        changed = tracker._process_game_record(
            "game-2",
            [
                {"account_id": 202, "nickname": "BravoLive", "placement": 1, "score": 45100},
                {"account_id": 303, "nickname": "Other", "placement": 2, "score": 27100},
                {"account_id": 101, "nickname": "AlphaLive", "placement": 3, "score": 19800},
                {"account_id": 404, "nickname": "Other2", "placement": 4, "score": 8000},
            ],
        )

        self.assertTrue(changed)

        bravo = tracker.players[202]
        self.assertEqual(bravo.game_results[0].placement, 1)
        self.assertEqual(bravo.game_results[0].uma, 15)
        self.assertEqual(bravo.game_results[0].adjusted_score, 35.1)

        alpha = tracker.players[101]
        self.assertEqual(alpha.game_results[0].placement, 3)
        self.assertEqual(alpha.game_results[0].uma, -5)
        self.assertEqual(alpha.game_results[0].adjusted_score, -10.2)

    def test_process_game_record_does_not_double_count_same_game(self) -> None:
        tracker = self.make_tracker()

        results = [
            {"account_id": 101, "nickname": "AlphaLive", "placement": 1, "score": 37000},
            {"account_id": 202, "nickname": "BravoLive", "placement": 4, "score": 12000},
        ]

        self.assertTrue(tracker._process_game_record("game-3", results))
        self.assertFalse(tracker._process_game_record("game-3", results))

        self.assertEqual(tracker.players[101].games_played, 1)
        self.assertEqual(tracker.players[202].games_played, 1)
        self.assertEqual(tracker.players[101].total_score, 27.0)
        self.assertEqual(tracker.players[202].total_score, -28.0)

    def test_update_scoring_recomputes_existing_results(self) -> None:
        tracker = self.make_tracker()

        tracker._process_game_record(
            "game-4",
            [
                {"account_id": 101, "nickname": "AlphaLive", "placement": 1, "score": 37000},
                {"account_id": 202, "nickname": "BravoLive", "placement": 4, "score": 12000},
            ],
        )

        tracker.update_scoring(
            uma_values=[20, 10, -10, -20],
            starting_points=30000,
            oka=5,
        )

        alpha = tracker.players[101]
        bravo = tracker.players[202]

        self.assertEqual(alpha.game_results[0].uma, 20)
        self.assertEqual(alpha.game_results[0].adjusted_score, 32.0)
        self.assertEqual(alpha.total_score, 32.0)

        self.assertEqual(bravo.game_results[0].uma, -20)
        self.assertEqual(bravo.game_results[0].adjusted_score, -38.0)
        self.assertEqual(bravo.total_score, -38.0)

    def test_manual_results_use_updated_scoring(self) -> None:
        tracker = self.make_tracker()
        tracker.update_scoring(
            uma_values=[12, 4, -4, -12],
            starting_points=25000,
            oka=0,
        )

        success = tracker.manual_add_result("Alpha", placement=2, raw_score=33000)

        self.assertTrue(success)
        result = tracker.players[101].game_results[0]
        self.assertEqual(result.uma, 4)
        self.assertEqual(result.adjusted_score, 12.0)


class GameTrackerLivePollingTests(unittest.IsolatedAsyncioTestCase):
    def make_tracker(self, client: FakeClient) -> GameTracker:
        tracker = GameTracker(
            client=client,
            players=[
                {"name": "Alpha", "account_id": 101},
                {"name": "Bravo", "account_id": 202},
            ],
            games_count=4,
            uma_values=[15, 5, -5, -15],
            starting_points=25000,
            oka=0,
        )
        tracker.contest_unique_id = 999
        return tracker

    async def test_seen_game_is_not_marked_live_again_if_live_list_lags(self) -> None:
        client = FakeClient(
            live_games=[
                {
                    "uuid": "game-1",
                    "players": [{"account_id": 101, "nickname": "AlphaLive"}],
                }
            ]
        )
        tracker = self.make_tracker(client)

        tracker._process_game_record(
            "game-1",
            [
                {"account_id": 101, "nickname": "AlphaLive", "placement": 2, "score": 32000},
                {"account_id": 303, "nickname": "Other", "placement": 1, "score": 41000},
            ],
        )

        changed = await tracker._poll_live_games()

        self.assertFalse(changed)
        self.assertEqual(tracker.players[101].status, PlayerStatus.IDLE)
        self.assertEqual(tracker.players[101].games_played, 1)
        self.assertEqual(tracker.players[101].current_points, None)
        self.assertEqual(tracker._observers, {})

    async def test_observer_end_keeps_live_state_until_record_arrives(self) -> None:
        client = FakeClient(
            live_games=[
                {
                    "uuid": "game-live",
                    "players": [{"account_id": 101, "nickname": "AlphaLive"}],
                }
            ]
        )
        tracker = self.make_tracker(client)
        observer = FakeObserver({101: 28700}, game_ended=True)
        tracker._observers["game-live"] = observer
        tracker._observed_players["game-live"] = {101}
        tracker.players[101].status = PlayerStatus.IN_GAME
        tracker.players[101].current_points = 28700

        changed = await tracker._poll_live_games()

        self.assertFalse(changed)
        self.assertEqual(tracker.players[101].status, PlayerStatus.IN_GAME)
        self.assertEqual(tracker.players[101].current_points, 28700)
        self.assertIn("game-live", tracker._pending_finished_game_uuids)
        self.assertNotIn("game-live", tracker._observers)
        self.assertTrue(observer.closed)

    async def test_observer_end_defers_streamed_final_result_while_game_is_live(self) -> None:
        client = FakeClient(
            live_games=[
                {
                    "uuid": "game-live",
                    "players": [{"account_id": 101, "nickname": "AlphaLive"}],
                }
            ]
        )
        tracker = self.make_tracker(client)
        observer = FakeObserver(
            {101: 28700},
            game_ended=True,
            final_results=[
                {
                    "account_id": 101,
                    "nickname": "AlphaLive",
                    "placement": 1,
                    "score": 39000,
                },
                {
                    "account_id": 303,
                    "nickname": "Other",
                    "placement": 2,
                    "score": 26000,
                },
            ],
        )
        tracker._observers["game-live"] = observer
        tracker._observed_players["game-live"] = {101}
        tracker.players[101].status = PlayerStatus.IN_GAME
        tracker.players[101].current_points = 28700

        changed = await tracker._poll_live_games()

        self.assertFalse(changed)
        self.assertEqual(tracker.players[101].status, PlayerStatus.IN_GAME)
        self.assertEqual(tracker.players[101].current_points, 28700)
        self.assertEqual(tracker.players[101].games_played, 0)
        self.assertIn("game-live", tracker._pending_finished_game_uuids)
        self.assertIn("game-live", tracker._deferred_game_results)
        self.assertNotIn("game-live", tracker._observers)
        self.assertTrue(observer.closed)

    async def test_deferred_streamed_final_result_applies_after_game_leaves_live_list(self) -> None:
        client = FakeClient(
            live_games=[
                {
                    "uuid": "game-live",
                    "players": [{"account_id": 101, "nickname": "AlphaLive"}],
                }
            ]
        )
        tracker = self.make_tracker(client)
        observer = FakeObserver(
            {101: 28700},
            game_ended=True,
            final_results=[
                {
                    "account_id": 101,
                    "nickname": "AlphaLive",
                    "placement": 1,
                    "score": 39000,
                },
                {
                    "account_id": 303,
                    "nickname": "Other",
                    "placement": 2,
                    "score": 26000,
                },
            ],
        )
        tracker._observers["game-live"] = observer
        tracker._observed_players["game-live"] = {101}
        tracker.players[101].status = PlayerStatus.IN_GAME
        tracker.players[101].current_points = 28700

        await tracker._poll_live_games()
        client.live_games = []

        changed = await tracker._poll_live_games()

        self.assertTrue(changed)
        self.assertEqual(tracker.players[101].status, PlayerStatus.IDLE)
        self.assertEqual(tracker.players[101].current_points, None)
        self.assertEqual(tracker.players[101].games_played, 1)
        self.assertEqual(tracker.players[101].game_results[0].placement, 1)
        self.assertEqual(tracker.players[101].game_results[0].raw_score, 39000)
        self.assertNotIn("game-live", tracker._pending_finished_game_uuids)
        self.assertNotIn("game-live", tracker._deferred_game_results)

    async def test_pending_finished_game_uses_tournament_log_when_available(self) -> None:
        client = FakeClient(
            game_records=[
                {
                    "uuid": "game-live",
                    "start_time": 1,
                    "end_time": 2,
                    "players": [
                        {
                            "account_id": 101,
                            "nickname": "AlphaLive",
                            "placement": 1,
                            "score": 39000,
                        },
                        {
                            "account_id": 303,
                            "nickname": "Other",
                            "placement": 2,
                            "score": 26000,
                        },
                    ],
                }
            ]
        )
        tracker = self.make_tracker(client)
        tracker._pending_finished_game_uuids.add("game-live")

        changed = await tracker._poll_game_records()

        self.assertTrue(changed)
        self.assertNotIn("game-live", tracker._pending_finished_game_uuids)
        self.assertEqual(tracker.players[101].games_played, 1)
        self.assertEqual(tracker.players[101].status, PlayerStatus.IDLE)
        self.assertEqual(tracker.players[101].game_results[0].placement, 1)
        self.assertEqual(tracker.players[101].game_results[0].raw_score, 39000)

    async def test_poll_game_records_defers_live_game_until_it_leaves_live_list(self) -> None:
        client = FakeClient(
            live_games=[
                {
                    "uuid": "game-live",
                    "players": [{"account_id": 101, "nickname": "AlphaLive"}],
                }
            ],
            game_records=[
                {
                    "uuid": "game-live",
                    "start_time": 1,
                    "end_time": 2,
                    "players": [
                        {
                            "account_id": 101,
                            "nickname": "AlphaLive",
                            "placement": 1,
                            "score": 39000,
                        },
                        {
                            "account_id": 303,
                            "nickname": "Other",
                            "placement": 2,
                            "score": 26000,
                        },
                    ],
                }
            ],
        )
        tracker = self.make_tracker(client)

        live_changed = await tracker._poll_live_games()
        records_changed = await tracker._poll_game_records()

        self.assertTrue(live_changed)
        self.assertFalse(records_changed)
        self.assertEqual(tracker.players[101].status, PlayerStatus.IN_GAME)
        self.assertEqual(tracker.players[101].games_played, 0)
        self.assertIn("game-live", tracker._deferred_game_results)

        client.live_games = []
        live_changed = await tracker._poll_live_games()

        self.assertTrue(live_changed)
        self.assertEqual(tracker.players[101].status, PlayerStatus.IDLE)
        self.assertEqual(tracker.players[101].games_played, 1)
        self.assertEqual(tracker.players[101].game_results[0].placement, 1)
        self.assertEqual(tracker.players[101].game_results[0].raw_score, 39000)
        self.assertNotIn("game-live", tracker._deferred_game_results)

    async def test_poll_game_records_uses_tournament_log_for_finished_game_uuid(self) -> None:
        client = FakeClient(
            game_records=[
                {
                    "uuid": "game-recorded",
                    "start_time": 1,
                    "end_time": 2,
                    "players": [
                        {
                            "account_id": 202,
                            "nickname": "BravoLive",
                            "placement": 1,
                            "score": 40100,
                        },
                        {
                            "account_id": 101,
                            "nickname": "AlphaLive",
                            "placement": 4,
                            "score": 14900,
                        },
                    ],
                }
            ],
        )
        tracker = self.make_tracker(client)

        changed = await tracker._poll_game_records()

        self.assertTrue(changed)
        self.assertEqual(tracker.players[202].game_results[0].raw_score, 40100)
        self.assertEqual(tracker.players[202].game_results[0].placement, 1)
        self.assertEqual(tracker.players[101].game_results[0].raw_score, 14900)
        self.assertEqual(tracker.players[101].game_results[0].placement, 4)
        self.assertIn("game-recorded", tracker._seen_game_uuids)

    async def test_poll_game_records_falls_back_to_summary_results(self) -> None:
        client = FakeClient(
            game_records=[
                {
                    "uuid": "game-summary-only",
                    "start_time": 1,
                    "end_time": 2,
                    "players": [
                        {
                            "account_id": 202,
                            "nickname": "BravoLive",
                            "placement": 1,
                            "score": 40100,
                        },
                        {
                            "account_id": 303,
                            "nickname": "Other",
                            "placement": 2,
                            "score": 25500,
                        },
                        {
                            "account_id": 404,
                            "nickname": "Other2",
                            "placement": 3,
                            "score": 19500,
                        },
                        {
                            "account_id": 101,
                            "nickname": "AlphaLive",
                            "placement": 4,
                            "score": 14900,
                        },
                    ],
                }
            ],
        )
        tracker = self.make_tracker(client)

        changed = await tracker._poll_game_records()

        self.assertTrue(changed)
        self.assertEqual(tracker.players[202].game_results[0].raw_score, 40100)
        self.assertEqual(tracker.players[202].game_results[0].placement, 1)
        self.assertEqual(tracker.players[101].game_results[0].raw_score, 14900)
        self.assertEqual(tracker.players[101].game_results[0].placement, 4)
        self.assertIn("game-summary-only", tracker._seen_game_uuids)

    async def test_poll_game_records_ignores_historical_results_before_tracking_start(self) -> None:
        client = FakeClient(
            game_records=[
                {
                    "uuid": "game-old",
                    "start_time": 1,
                    "end_time": 100,
                    "players": [
                        {
                            "account_id": 101,
                            "nickname": "AlphaLive",
                            "placement": 1,
                            "score": 39000,
                        }
                    ],
                }
            ]
        )
        tracker = self.make_tracker(client)
        tracker._tracking_started_at = 200

        changed = await tracker._poll_game_records()

        self.assertFalse(changed)
        self.assertEqual(tracker.players[101].games_played, 0)
        self.assertIn("game-old", tracker._seen_game_uuids)

    async def test_poll_game_records_keeps_results_after_tracking_start(self) -> None:
        client = FakeClient(
            game_records=[
                {
                    "uuid": "game-new",
                    "start_time": 1,
                    "end_time": 250,
                    "players": [
                        {
                            "account_id": 101,
                            "nickname": "AlphaLive",
                            "placement": 2,
                            "score": 32000,
                        },
                        {
                            "account_id": 303,
                            "nickname": "Other",
                            "placement": 1,
                            "score": 41000,
                        },
                    ],
                }
            ]
        )
        tracker = self.make_tracker(client)
        tracker._tracking_started_at = 200

        changed = await tracker._poll_game_records()

        self.assertTrue(changed)
        self.assertEqual(tracker.players[101].games_played, 1)
        self.assertEqual(tracker.players[101].game_results[0].placement, 2)
        self.assertEqual(tracker.players[101].game_results[0].raw_score, 32000)

    async def test_start_backfills_recent_logs_and_keeps_current_game_live(self) -> None:
        client = FakeClient(
            live_games=[
                {
                    "uuid": "game-3-live",
                    "players": [{"account_id": 101, "nickname": "AlphaLive"}],
                }
            ],
            game_records=[
                {
                    "uuid": "game-old",
                    "start_time": 1,
                    "end_time": 150,
                    "players": [
                        {
                            "account_id": 101,
                            "nickname": "AlphaLive",
                            "placement": 1,
                            "score": 38000,
                        }
                    ],
                },
                {
                    "uuid": "game-1",
                    "start_time": 1,
                    "end_time": 210,
                    "players": [
                        {
                            "account_id": 101,
                            "nickname": "AlphaLive",
                            "placement": 2,
                            "score": 32000,
                        },
                        {
                            "account_id": 303,
                            "nickname": "Other",
                            "placement": 1,
                            "score": 41000,
                        },
                    ],
                },
                {
                    "uuid": "game-2",
                    "start_time": 1,
                    "end_time": 220,
                    "players": [
                        {
                            "account_id": 101,
                            "nickname": "AlphaLive",
                            "placement": 4,
                            "score": 11000,
                        },
                        {
                            "account_id": 303,
                            "nickname": "Other",
                            "placement": 1,
                            "score": 47000,
                        },
                    ],
                },
            ],
        )
        tracker = self.make_tracker(client)

        try:
            await tracker.start(contest_id=123, tracking_started_at=200)

            alpha = tracker.players[101]
            self.assertEqual(tracker.tracking_started_at, 200)
            self.assertEqual(alpha.games_played, 2)
            self.assertEqual(
                [result.game_uuid for result in alpha.game_results],
                ["game-1", "game-2"],
            )
            self.assertEqual(alpha.status, PlayerStatus.IN_GAME)
            self.assertEqual(alpha.current_points, None)
            self.assertIn("game-old", tracker._seen_game_uuids)
            self.assertEqual(client.entered_contests, [999])
        finally:
            tracker.stop()
            await asyncio.sleep(0)


class GameTrackerTeamsTests(unittest.TestCase):
    def make_tracker(self) -> GameTracker:
        return GameTracker(
            client=None,
            players=[],
            teams=[
                {
                    "name": "Team Red",
                    "group": "Semi-final A",
                    "players": [
                        {"name": "Alpha", "account_id": 101},
                        {"name": "Bravo", "account_id": 202},
                    ],
                },
                {
                    "name": "Team Blue",
                    "group": "Semi-final A",
                    "players": [
                        {"name": "Charlie", "account_id": 303},
                        {"name": "Delta", "account_id": 404},
                    ],
                },
                {
                    "name": "Team Green",
                    "group": "Semi-final B",
                    "players": [
                        {"name": "Echo", "account_id": 505},
                        {"name": "Foxtrot", "account_id": 606},
                    ],
                },
            ],
            games_count=4,
            uma_values=[15, 5, -5, -15],
            starting_points=25000,
            oka=0,
        )

    def test_team_state_aggregates_player_totals_and_preserves_groups(self) -> None:
        tracker = self.make_tracker()

        tracker._process_game_record(
            "game-1",
            [
                {"account_id": 303, "nickname": "CharlieLive", "placement": 1, "score": 41000},
                {"account_id": 101, "nickname": "AlphaLive", "placement": 2, "score": 32000},
                {"account_id": 202, "nickname": "BravoLive", "placement": 3, "score": 21000},
                {"account_id": 404, "nickname": "DeltaLive", "placement": 4, "score": 6000},
            ],
        )
        tracker._process_game_record(
            "game-2",
            [
                {"account_id": 505, "nickname": "EchoLive", "placement": 1, "score": 39000},
                {"account_id": 707, "nickname": "Other", "placement": 2, "score": 27000},
                {"account_id": 808, "nickname": "Other2", "placement": 3, "score": 18000},
                {"account_id": 606, "nickname": "FoxtrotLive", "placement": 4, "score": 16000},
            ],
        )

        state = tracker.get_state().to_dict()

        self.assertTrue(state["has_teams"])
        self.assertEqual(
            [team["name"] for team in state["teams"]],
            ["Team Red", "Team Blue", "Team Green"],
        )

        red_team = state["teams"][0]
        self.assertEqual(red_team["group"], "Semi-final A")
        self.assertEqual(red_team["games_played"], 2)
        self.assertEqual(red_team["games_target"], 8)
        self.assertEqual(red_team["total_score"], 3.0)
        self.assertEqual([player["name"] for player in red_team["players"]], ["Alpha", "Bravo"])

        green_team = state["teams"][2]
        self.assertEqual(green_team["group"], "Semi-final B")
        self.assertEqual(green_team["total_score"], 5.0)

    def test_manual_result_accepts_qualified_team_player_name(self) -> None:
        tracker = self.make_tracker()

        success = tracker.manual_add_result("Team Red / Alpha", placement=1, raw_score=37000)

        self.assertTrue(success)
        state = tracker.get_state().to_dict()
        red_team = state["teams"][0]
        self.assertEqual(red_team["total_score"], 27.0)
        self.assertEqual(red_team["players"][0]["qualified_name"], "Team Red / Alpha")


class GameTrackerTeamLiveScoreTests(unittest.IsolatedAsyncioTestCase):
    def make_tracker(self, client: FakeClient) -> GameTracker:
        tracker = GameTracker(
            client=client,
            players=[],
            teams=[
                {
                    "name": "Team Red",
                    "group": "Semi-final A",
                    "players": [
                        {"name": "Alpha", "account_id": 101},
                        {"name": "Bravo", "account_id": 202},
                    ],
                },
                {
                    "name": "Team Blue",
                    "group": "Semi-final A",
                    "players": [
                        {"name": "Charlie", "account_id": 303},
                        {"name": "Delta", "account_id": 404},
                    ],
                },
            ],
            games_count=4,
            uma_values=[15, 5, -5, -15],
            starting_points=25000,
            oka=0,
        )
        tracker.contest_unique_id = 999
        return tracker

    async def test_live_game_adds_provisional_team_score_without_changing_confirmed_score(self) -> None:
        client = FakeClient(
            live_games=[
                {
                    "uuid": "game-live",
                    "players": [{"account_id": 101, "nickname": "AlphaLive"}],
                }
            ],
            observers={
                "game-live": FakeObserver(
                    {
                        101: 36100,
                        303: 25400,
                        404: 22100,
                        505: 16400,
                    }
                )
            },
        )
        tracker = self.make_tracker(client)
        tracker.manual_add_result("Bravo", placement=2, raw_score=32000)

        changed = await tracker._poll_live_games()

        self.assertTrue(changed)
        state = tracker.get_state().to_dict()
        team_red = state["teams"][0]
        alpha = next(player for player in state["players"] if player["account_id"] == 101)

        self.assertEqual(team_red["total_score"], 12.0)
        self.assertEqual(team_red["provisional_score"], 38.1)
        self.assertEqual(team_red["total_uma"], 5)
        self.assertEqual(team_red["provisional_uma"], 20)
        self.assertEqual(alpha["current_placement"], 1)
        self.assertEqual(alpha["provisional_score"], 26.1)
        self.assertEqual(alpha["provisional_uma"], 15)


class GameTrackerTeamHistoricalBackfillTests(unittest.IsolatedAsyncioTestCase):
    def make_tracker(self, client: FakeClient) -> GameTracker:
        return GameTracker(
            client=client,
            players=[],
            teams=[
                {
                    "name": "Team Red",
                    "group": "Semi-final A",
                    "players": [
                        {"name": "Alpha", "account_id": 101},
                        {"name": "Bravo", "account_id": 202},
                    ],
                },
                {
                    "name": "Team Blue",
                    "group": "Semi-final A",
                    "players": [
                        {"name": "Charlie", "account_id": 303},
                        {"name": "Delta", "account_id": 404},
                    ],
                },
            ],
            games_count=4,
            uma_values=[15, 5, -5, -15],
            starting_points=25000,
            oka=0,
        )

    async def test_start_backfills_team_results_from_tracking_start(self) -> None:
        client = FakeClient(
            game_records=[
                {
                    "uuid": "game-old",
                    "start_time": 1,
                    "end_time": 190,
                    "players": [
                        {"account_id": 101, "nickname": "AlphaLive", "placement": 1, "score": 41000},
                        {"account_id": 303, "nickname": "CharlieLive", "placement": 2, "score": 29000},
                    ],
                },
                {
                    "uuid": "game-1",
                    "start_time": 1,
                    "end_time": 210,
                    "players": [
                        {"account_id": 101, "nickname": "AlphaLive", "placement": 2, "score": 32000},
                        {"account_id": 303, "nickname": "CharlieLive", "placement": 1, "score": 41000},
                    ],
                },
                {
                    "uuid": "game-2",
                    "start_time": 1,
                    "end_time": 220,
                    "players": [
                        {"account_id": 202, "nickname": "BravoLive", "placement": 1, "score": 39000},
                        {"account_id": 404, "nickname": "DeltaLive", "placement": 4, "score": 16000},
                    ],
                },
            ],
        )
        tracker = self.make_tracker(client)

        try:
            await tracker.start(contest_id=123, tracking_started_at=200)

            state = tracker.get_state().to_dict()
            team_red = state["teams"][0]
            team_blue = state["teams"][1]

            self.assertEqual(tracker.tracking_started_at, 200)
            self.assertEqual(team_red["games_played"], 2)
            self.assertEqual(team_red["total_score"], 41.0)
            self.assertEqual(team_red["provisional_score"], 41.0)
            self.assertEqual(team_red["total_uma"], 20)
            self.assertEqual(team_red["provisional_uma"], 20)
            self.assertEqual(team_blue["games_played"], 2)
            self.assertEqual(team_blue["total_score"], 7.0)
            self.assertEqual(team_blue["provisional_score"], 7.0)
            self.assertEqual(team_blue["total_uma"], 0)
            self.assertIn("game-old", tracker._seen_game_uuids)
            self.assertEqual(client.entered_contests, [999])
        finally:
            tracker.stop()
            await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()

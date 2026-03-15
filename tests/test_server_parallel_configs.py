from __future__ import annotations

import os
import unittest
from unittest import mock

import src.server as server


class ServerParallelConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.original_player_tracker = server.player_tracker
        self.original_team_tracker = server.team_tracker

    def tearDown(self) -> None:
        server.player_tracker = self.original_player_tracker
        server.team_tracker = self.original_team_tracker

    def test_player_and_team_configs_load_separately(self) -> None:
        self.assertTrue(server.player_config)
        self.assertTrue(server.team_config)

        self.assertEqual(len(server._configured_team_entries(server.player_config)), 0)
        self.assertEqual(len(server._configured_player_entries(server.player_config)), 4)

        self.assertEqual(len(server._configured_team_entries(server.team_config)), 8)
        self.assertEqual(len(server._configured_player_entries(server.team_config)), 34)

    def test_demo_trackers_initialize_in_parallel(self) -> None:
        server._initialize_demo_trackers()

        player_snapshot = server._tracker_snapshot(server.player_tracker)
        team_snapshot = server._tracker_snapshot(server.team_tracker)

        self.assertFalse(player_snapshot["has_teams"])
        self.assertEqual(len(player_snapshot["players"]), 4)
        self.assertEqual(player_snapshot["teams"], [])

        self.assertTrue(team_snapshot["has_teams"])
        self.assertEqual(len(team_snapshot["players"]), 34)
        self.assertEqual(len(team_snapshot["teams"]), 8)

    def test_team_contest_id_defaults_to_player_contest_id(self) -> None:
        player_contest_id = server._configured_contest_id(server.player_config)
        team_contest_id = server._configured_contest_id(
            server.team_config,
            player_contest_id,
        )

        self.assertEqual(player_contest_id, 970393)
        self.assertEqual(team_contest_id, player_contest_id)

    def test_legacy_config_override_maps_to_team_config_only(self) -> None:
        with mock.patch.dict(os.environ, {server.LEGACY_CONFIG_ENV_VAR: "legacy-team.yaml"}, clear=False):
            player_path = server._resolve_config_path(server.PLAYER_CONFIG_ENV_VAR, "config.yaml")
            team_path = server._resolve_config_path(
                server.TEAM_CONFIG_ENV_VAR,
                "config.semifinals.yaml",
                legacy_fallback=True,
            )

        self.assertEqual(player_path.name, "config.yaml")
        self.assertEqual(team_path.name, "legacy-team.yaml")


if __name__ == "__main__":
    unittest.main()

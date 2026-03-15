from __future__ import annotations

import enum
from dataclasses import dataclass, field


class PlayerStatus(str, enum.Enum):
    IDLE = "idle"
    IN_GAME = "in_game"
    FINISHED = "finished"


@dataclass
class GameResult:
    """Result of a single completed game for a player."""

    game_uuid: str
    placement: int  # 1-4
    raw_score: int  # Final point count (e.g. 45600)
    uma: float  # Placement bonus applied
    adjusted_score: float  # (raw_score - starting_points) / 1000 + uma

    def to_dict(self) -> dict:
        return {
            "game_uuid": self.game_uuid,
            "placement": self.placement,
            "raw_score": self.raw_score,
            "uma": self.uma,
            "adjusted_score": self.adjusted_score,
        }


@dataclass
class PlayerState:
    """Tracked state for a single team player."""

    name: str
    account_id: int
    nickname: str | None = None
    team_id: str | None = None
    team_name: str | None = None
    team_group: str | None = None
    team_index: int = 0
    roster_index: int = 0
    status: PlayerStatus = PlayerStatus.IDLE
    current_game_uuid: str | None = None
    current_points: int | None = None
    current_round: str | None = None
    current_placement: int | None = None
    provisional_uma: float | None = None
    provisional_score: float | None = None
    game_results: list[GameResult] = field(default_factory=list)

    @property
    def total_score(self) -> float:
        return sum(g.adjusted_score for g in self.game_results)

    @property
    def games_played(self) -> int:
        return len(self.game_results)

    @property
    def display_name(self) -> str:
        return self.nickname or self.name

    @property
    def qualified_name(self) -> str:
        if self.team_name:
            return f"{self.team_name} / {self.display_name}"
        return self.display_name

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "nickname": self.nickname,
            "display_name": self.display_name,
            "qualified_name": self.qualified_name,
            "account_id": self.account_id,
            "team_id": self.team_id,
            "team_name": self.team_name,
            "team_group": self.team_group,
            "status": self.status.value,
            "current_points": self.current_points,
            "current_round": self.current_round,
            "current_placement": self.current_placement,
            "provisional_uma": self.provisional_uma,
            "provisional_score": self.provisional_score,
            "games": [g.to_dict() for g in self.game_results],
            "games_played": self.games_played,
            "total_score": round(self.total_score, 1),
        }


@dataclass
class TeamState:
    """Tracked aggregate state for a team made up of multiple players."""

    id: str
    name: str
    players: list[PlayerState] = field(default_factory=list)
    group: str | None = None
    group_index: int = 0
    sort_index: int = 0
    games_per_player: int = 0

    @property
    def total_score(self) -> float:
        return sum(player.total_score for player in self.players)

    @property
    def total_uma(self) -> float:
        return sum(
            result.uma
            for player in self.players
            for result in player.game_results
        )

    @property
    def provisional_uma(self) -> float:
        return self.total_uma + sum(
            player.provisional_uma or 0
            for player in self.players
        )

    @property
    def provisional_score(self) -> float:
        return self.total_score + sum(
            player.provisional_score or 0
            for player in self.players
        )

    @property
    def games_played(self) -> int:
        return sum(player.games_played for player in self.players)

    @property
    def games_target(self) -> int:
        return len(self.players) * self.games_per_player

    @property
    def active_players(self) -> int:
        return sum(
            1
            for player in self.players
            if player.status == PlayerStatus.IN_GAME
        )

    @property
    def finished_players(self) -> int:
        return sum(
            1
            for player in self.players
            if player.status == PlayerStatus.FINISHED
        )

    @property
    def current_rounds(self) -> list[str]:
        rounds = {
            player.current_round
            for player in self.players
            if player.status == PlayerStatus.IN_GAME and player.current_round
        }
        return sorted(rounds)

    @property
    def status(self) -> PlayerStatus:
        if any(player.status == PlayerStatus.IN_GAME for player in self.players):
            return PlayerStatus.IN_GAME
        if self.players and all(
            player.status == PlayerStatus.FINISHED for player in self.players
        ):
            return PlayerStatus.FINISHED
        return PlayerStatus.IDLE

    def to_dict(self) -> dict:
        roster = sorted(
            self.players,
            key=lambda player: (player.roster_index, player.name.lower()),
        )
        return {
            "id": self.id,
            "name": self.name,
            "group": self.group,
            "status": self.status.value,
            "player_count": len(self.players),
            "games_played": self.games_played,
            "games_target": self.games_target,
            "active_players": self.active_players,
            "finished_players": self.finished_players,
            "current_rounds": self.current_rounds,
            "players": [player.to_dict() for player in roster],
            "total_score": round(self.total_score, 1),
            "total_uma": round(self.total_uma, 1),
            "provisional_uma": round(self.provisional_uma, 1),
            "provisional_score": round(self.provisional_score, 1),
        }


@dataclass
class TournamentState:
    """Full tournament state broadcast to the overlay."""

    players: list[PlayerState]
    games_count: int
    uma_values: list[float]
    teams: list[TeamState] = field(default_factory=list)
    observer_level: int | None = None
    observer_error: str | None = None

    def to_dict(self) -> dict:
        # Sort players by total score descending
        sorted_players = sorted(
            self.players, key=lambda p: p.total_score, reverse=True
        )
        sorted_teams = sorted(
            self.teams,
            key=lambda team: (team.group_index, -team.total_score, team.sort_index),
        )
        return {
            "players": [p.to_dict() for p in sorted_players],
            "teams": [team.to_dict() for team in sorted_teams],
            "has_teams": bool(sorted_teams),
            "games_count": self.games_count,
            "uma": self.uma_values,
            "observer_level": self.observer_level,
            "observer_error": self.observer_error,
        }

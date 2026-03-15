from __future__ import annotations

from types import SimpleNamespace
import unittest

from src.majsoul_client import MajsoulClient


def _account(seat: int, account_id: int, nickname: str) -> SimpleNamespace:
    return SimpleNamespace(seat=seat, account_id=account_id, nickname=nickname)


def _result_player(
    seat: int,
    part_point_1: int,
    *,
    total_point: int = 0,
    grading_score: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        seat=seat,
        part_point_1=part_point_1,
        total_point=total_point,
        grading_score=grading_score,
    )


def _record(
    uuid: str,
    *,
    start_time: int,
    end_time: int,
    players: list[tuple[int, int, str, int]],
) -> SimpleNamespace:
    accounts = [_account(seat, account_id, nickname) for seat, account_id, nickname, _score in players]
    result_players = [
        _result_player(seat, score)
        for seat, _account_id, _nickname, score in players
    ]
    return SimpleNamespace(
        uuid=uuid,
        start_time=start_time,
        end_time=end_time,
        accounts=accounts,
        result=SimpleNamespace(players=result_players),
    )


def _response(next_index: int, records: list[SimpleNamespace]) -> SimpleNamespace:
    return SimpleNamespace(
        error=SimpleNamespace(code=0),
        next_index=next_index,
        record_list=records,
    )


class FakeLobby:
    def __init__(self, responses: list[SimpleNamespace]):
        self._responses = list(responses)
        self.last_indexes: list[int] = []

    async def fetch_customized_contest_game_records(self, req) -> SimpleNamespace:
        self.last_indexes.append(req.last_index)
        if not self._responses:
            raise AssertionError("Unexpected extra contest record request")
        return self._responses.pop(0)


class MajsoulClientContestRecordsTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_contest_game_records_follows_next_index_until_exhausted(self) -> None:
        lobby = FakeLobby([
            _response(
                7,
                [
                    _record(
                        "game-newer",
                        start_time=200,
                        end_time=300,
                        players=[
                            (0, 101, "Alpha", 41000),
                            (1, 202, "Bravo", 27000),
                            (2, 303, "Charlie", 19000),
                            (3, 404, "Delta", 13000),
                        ],
                    )
                ],
            ),
            _response(
                0,
                [
                    _record(
                        "game-older",
                        start_time=100,
                        end_time=180,
                        players=[
                            (0, 101, "Alpha", 30300),
                            (1, 202, "Bravo", 25000),
                            (2, 303, "Charlie", 24000),
                            (3, 404, "Delta", 20700),
                        ],
                    )
                ],
            ),
        ])

        client = MajsoulClient()

        async def noop() -> None:
            return None

        client._ensure_connected = noop  # type: ignore[method-assign]
        client.lobby = lobby  # type: ignore[assignment]

        records = await client.fetch_contest_game_records(unique_id=12345)

        self.assertEqual(lobby.last_indexes, [0, 7])
        self.assertEqual([record["uuid"] for record in records], ["game-newer", "game-older"])
        self.assertEqual(records[1]["players"][0]["nickname"], "Alpha")
        self.assertEqual(records[1]["players"][0]["score"], 30300)

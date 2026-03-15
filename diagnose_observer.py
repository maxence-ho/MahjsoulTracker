"""Diagnostic script to debug game server endpoint for authObserve."""

import asyncio
import logging
import random
import aiohttp
import yaml
from pathlib import Path

from src.majsoul_client import MajsoulClient, MSRPCChannel, FastTest

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yaml"
with open(CONFIG_PATH) as f:
    config = yaml.safe_load(f)


async def main():
    client = MajsoulClient(server=config.get("server", "en"))
    await client.connect()

    # Login
    uid = config.get("yostar_uid", "")
    token = config.get("yostar_token", "")
    if uid and token:
        account = await client.login_with_yostar_token(uid, token)
        logger.info("Logged in as: %s (ID: %s)", account["nickname"], account["account_id"])
    else:
        logger.error("No credentials in config")
        return

    # Get contest info
    contest_id = config.get("contest_id", 0)
    contest_info = await client.fetch_contest_info(contest_id)
    unique_id = contest_info["unique_id"]

    # Enter contest
    try:
        await client.enter_contest(unique_id)
    except Exception:
        pass

    # Fetch live games
    live_games = await client.fetch_live_games(unique_id)
    if not live_games:
        logger.info("No live games found.")
        await client.close()
        return

    game = live_games[0]
    game_uuid = game["uuid"]
    logger.info("Game: %s, Players: %s", game_uuid, [p["nickname"] for p in game["players"]])

    # Dump config.json to see all available endpoints
    logger.info("=== Dumping Mahjong Soul config ===")
    async with aiohttp.ClientSession() as session:
        base = client.server_config["base_url"]
        rand = random.randint(0, 2**31)

        async with session.get(f"{base}/version.json?randv={rand}") as resp:
            version_data = await resp.json()
        version = version_data["version"]

        async with session.get(f"{base}/resversion{version}.json?randv={rand}") as resp:
            resver_data = await resp.json()

        config_prefix = resver_data["res"]["config.json"]["prefix"]
        async with session.get(f"{base}/{config_prefix}/config.json") as resp:
            config_data = await resp.json()

        import json
        logger.info("Full config.json ip section:\n%s", json.dumps(config_data["ip"], indent=2))

        # Check if there are region_urls for game servers
        ip_config = config_data["ip"][0]
        if "region_urls" in ip_config:
            logger.info("region_urls found!")
            for ru in ip_config["region_urls"]:
                logger.info("  region_url: %s", ru)
                # Try getting game servers
                async with session.get(ru["url"] + "?service=ws-game&protocol=ws&ssl=true") as resp:
                    game_servers = await resp.json()
                    logger.info("  game servers: %s", game_servers)

        # Try getting game servers via different service parameter
        if "gateways" in ip_config:
            logger.info("gateways found:")
            for gw in ip_config["gateways"]:
                logger.info("  gateway: %s", gw)

    # Get OB token
    from ms.protocol_pb2 import ReqFetchOBToken
    req = ReqFetchOBToken()
    req.uuid = game_uuid
    res = await client.lobby.fetch_ob_token(req)
    ob_token = res.token
    logger.info("OB token: %s, delay: %s", ob_token, res.delay)

    # Try fetchGameLiveInfo to get game server location
    logger.info("=== fetchGameLiveInfo for server location ===")
    try:
        from ms.protocol_pb2 import ReqGameLiveInfo
        req = ReqGameLiveInfo()
        req.game_uuid = game_uuid
        res = await client.lobby.fetch_game_live_info(req)
        logger.info("fetchGameLiveInfo response fields:")
        for field in res.DESCRIPTOR.fields:
            val = getattr(res, field.name)
            val_str = repr(val)[:200]
            logger.info("  %s = %s", field.name, val_str)
        if res.live_head:
            logger.info("live_head fields:")
            for field in res.live_head.DESCRIPTOR.fields:
                val = getattr(res.live_head, field.name)
                logger.info("  %s = %s", field.name, repr(val)[:200])
    except Exception as e:
        logger.error("fetchGameLiveInfo FAILED: %s", e)

    # Try fetching contest live list raw to see location data
    logger.info("=== Raw contest live list ===")
    try:
        from ms.protocol_pb2 import ReqFetchCustomizedContestGameLiveList
        req = ReqFetchCustomizedContestGameLiveList()
        req.unique_id = unique_id
        res = await client.lobby.fetch_customized_contest_game_live_list(req)
        for g in res.live_list:
            logger.info("Live game fields:")
            for field in g.DESCRIPTOR.fields:
                val = getattr(g, field.name)
                val_str = repr(val)[:200]
                logger.info("  %s = %s", field.name, val_str)
    except Exception as e:
        logger.error("FAILED: %s", e)

    # Try createGameObserveAuth to see what location it would return (even if it fails)
    logger.info("=== createGameObserveAuth (for location info) ===")
    try:
        from ms.protocol_pb2 import ReqCreateGameObserveAuth
        req = ReqCreateGameObserveAuth()
        req.game_uuid = game_uuid
        res = await client.lobby.create_game_observe_auth(req)
        logger.info("createGameObserveAuth response:")
        for field in res.DESCRIPTOR.fields:
            val = getattr(res, field.name)
            logger.info("  %s = %s", field.name, repr(val)[:200])
    except Exception as e:
        logger.error("createGameObserveAuth FAILED: %s", e)

    # Try connecting to the main gateway (engs.mahjongsoul.com) rather than backup
    logger.info("=== Try authObserve on main gateway ===")
    try:
        main_endpoint = "wss://engs.mahjongsoul.com/gateway"
        ms_host = client.server_config["base_url"].replace("https://", "")
        game_channel = MSRPCChannel(main_endpoint)
        await game_channel.connect(ms_host)

        fast_test = FastTest(game_channel)
        from ms.protocol_pb2 import ReqAuthObserve
        req = ReqAuthObserve()
        req.token = ob_token
        res = await fast_test.auth_observe(req)
        if res.error and res.error.code:
            logger.error("authObserve on main gateway FAILED: %s", res.error)
        else:
            logger.info("authObserve on main gateway SUCCESS!")
        await game_channel._ws.close()
    except Exception as e:
        logger.error("Main gateway attempt FAILED: %s", e)

    await client.close()


if __name__ == "__main__":
    asyncio.run(main())

"""Debug script: test alternative login methods since oauth2Auth is disabled (error 151)."""
import asyncio
import random
import logging
import aiohttp

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("auth_debug")

async def get_gateway():
    """Get game version and gateway endpoint."""
    base = "https://mahjongsoul.game.yo-star.com"
    rand = random.randint(0, 2**31)
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{base}/version.json?randv={rand}") as resp:
            version = (await resp.json())["version"]
        async with session.get(f"{base}/resversion{version}.json?randv={rand}") as resp:
            resver = await resp.json()
        config_prefix = resver["res"]["config.json"]["prefix"]
        async with session.get(f"{base}/{config_prefix}/config.json") as resp:
            config = await resp.json()
        gw = random.choice(config["ip"][0]["gateways"])["url"]
        endpoint = gw.replace("https://", "wss://") + "/gateway"
    return version, endpoint


async def test_login_method(version, endpoint, account, password, auth_type):
    """Test the 'login' RPC method (not oauth2Auth)."""
    from ms.base import MSRPCChannel
    from ms.rpc import Lobby
    from ms.protocol_pb2 import ReqLogin

    channel = MSRPCChannel(endpoint)
    await channel.connect("mahjongsoul.game.yo-star.com")
    lobby = Lobby(channel)

    req = ReqLogin()
    req.account = account
    req.password = password
    req.type = auth_type
    req.reconnect = False
    req.device.is_browser = True
    req.device.software = "Chrome"
    req.device.platform = "pc"
    req.device.os = "mac"
    req.device.sale_platform = "web"
    req.random_key = str(random.randint(0, 2**32 - 1))
    req.client_version_string = f"web-{version}"
    req.currency_platforms.append(2)
    req.currency_platforms.append(9)

    try:
        res = await lobby.login(req)
        err = res.error
        code = err.code if err else 0
        json_param = err.json_param if err and err.json_param else ""
        u32 = list(err.u32_params) if err and err.u32_params else []
        str_p = list(err.str_params) if err and err.str_params else []
        account_id = res.account_id if hasattr(res, 'account_id') else 0
        access_token = res.access_token if hasattr(res, 'access_token') and res.access_token else ""
        logger.info("login(type=%d, acct=%s) → error=%d, json=%s, u32=%s, str=%s, account_id=%s, token=%s",
                     auth_type, account[:15], code, json_param, u32, str_p, account_id,
                     access_token[:20] if access_token else "")
        return code
    except Exception as e:
        logger.error("login(type=%d) exception: %s", auth_type, e)
        return -1
    finally:
        try:
            await channel._ws.close()
        except:
            pass


async def test_email_login(version, endpoint, email, password):
    """Test the 'emailLogin' RPC method."""
    from ms.base import MSRPCChannel
    from ms.rpc import Lobby
    from ms.protocol_pb2 import ReqEmailLogin

    channel = MSRPCChannel(endpoint)
    await channel.connect("mahjongsoul.game.yo-star.com")
    lobby = Lobby(channel)

    req = ReqEmailLogin()
    req.email = email
    req.password = password
    req.reconnect = False
    req.device.is_browser = True
    req.device.software = "Chrome"
    req.device.platform = "pc"
    req.device.os = "mac"
    req.device.sale_platform = "web"
    req.random_key = str(random.randint(0, 2**32 - 1))
    req.currency_platforms.append(2)
    req.currency_platforms.append(9)

    try:
        res = await lobby.email_login(req)
        err = res.error
        code = err.code if err else 0
        json_param = err.json_param if err and err.json_param else ""
        account_id = res.account_id if hasattr(res, 'account_id') else 0
        logger.info("emailLogin(email=%s) → error=%d, json=%s, account_id=%s",
                     email[:15], code, json_param, account_id)
        return code
    except Exception as e:
        logger.error("emailLogin exception: %s", e)
        return -1
    finally:
        try:
            await channel._ws.close()
        except:
            pass


async def main():
    version, endpoint = await get_gateway()
    logger.info("Game version: %s, Gateway: %s", version, endpoint)

    # Test 'login' method with fake credentials and different types
    # to see what errors we get (compare to oauth2Auth's blanket 151)
    logger.info("=== Testing 'login' RPC method ===")
    for auth_type in [22, 7, 8, 20, 0]:
        await test_login_method(version, endpoint, "test_uid", "test_token", auth_type)
        await asyncio.sleep(0.3)

    # Test emailLogin with fake credentials
    logger.info("=== Testing 'emailLogin' RPC method ===")
    await test_email_login(version, endpoint, "test@test.com", "test_password")

    logger.info("=== Done ===")

asyncio.run(main())

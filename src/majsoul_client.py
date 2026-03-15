"""Majsoul WebSocket client — connects to the Mahjong Soul API and fetches contest data."""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Callable

import hashlib
import json as json_mod
import time
import uuid

import base64

import aiohttp
import websockets
from ms.base import MSRPCChannel
from ms.protocol_pb2 import Wrapper
from ms.rpc import Lobby, FastTest

# Yostar SDK v4 signing key for Mahjong Soul (from yostar_sdk/en/index.js.txt)
YOSTAR_SDK_SIGNING_KEY = "347467131a466f6865d7f2662e38841fbe2adb23"

logger = logging.getLogger(__name__)

# Server endpoints
SERVERS = {
    "en": {
        "base_url": "https://mahjongsoul.game.yo-star.com",
        "api_url": "https://passport.mahjongsoul.com/user/login",
        "yostar_sdk": "https://en-sdk-api.yostarplat.com",
    },
    "jp": {
        "base_url": "https://game.mahjongsoul.com",
        "api_url": "https://passport.mahjongsoul.com/user/login",
        "yostar_sdk": "https://jp-sdk-api.yostarplat.com",
    },
    "cn": {
        "base_url": "https://game.maj-soul.com/1",
        "api_url": None,
        "yostar_sdk": None,
    },
}

# Auth types seen in the live web client for Yostar/oauth login.
OAUTH_LOGIN_TYPES = [7, 13, 20, 8, 22]
DIRECT_LOGIN_TYPES = [22, 8, 20, 7, 13]


class MajsoulClient:
    """Connects to Mahjong Soul servers and provides contest data access."""

    def __init__(self, server: str = "en"):
        if server not in SERVERS:
            raise ValueError(f"Unknown server: {server}. Use: {list(SERVERS.keys())}")
        self.server = server
        self.server_config = SERVERS[server]
        self.channel: MSRPCChannel | None = None
        self.lobby: Lobby | None = None
        self._connected = False
        self._version: str = ""
        self._game_config: dict = {}  # full config.json from server
        self._lobby_endpoint: str = ""  # gateway the lobby is connected to
        # Stored credentials for automatic re-authentication after reconnect
        self._last_login: dict | None = None  # {"method": ..., ...credentials}

    async def connect(self) -> None:
        """Establish WebSocket connection to Majsoul servers."""
        logger.info("Connecting to Majsoul %s server...", self.server)

        async with aiohttp.ClientSession() as session:
            # 1. Fetch version info
            rand = random.randint(0, 2**31)
            base = self.server_config["base_url"]
            async with session.get(f"{base}/version.json?randv={rand}") as resp:
                version_data = await resp.json()
            version = version_data["version"]
            self._version = version
            logger.info("Game version: %s", version)

            # 2. Fetch resversion to get config prefix
            resver_url = f"{base}/resversion{version}.json?randv={rand}"
            async with session.get(resver_url) as resp:
                resver_data = await resp.json()

            # 3. Get config prefix and fetch config
            config_prefix = resver_data["res"]["config.json"]["prefix"]
            config_url = f"{base}/{config_prefix}/config.json"
            async with session.get(config_url) as resp:
                config_data = await resp.json()

            self._game_config = config_data
            logger.info("Config loaded: %s", list(config_data.keys()))

            # Log auth-relevant config
            if "yo_service_url" in config_data:
                logger.info("Yostar service URL: %s", config_data["yo_service_url"])
            if "yostar_sdk_4_pid" in config_data:
                logger.info("Yostar SDK4 PID: %s", config_data["yostar_sdk_4_pid"])

            # 4. Get gateway endpoint
            ip_config = config_data["ip"][0]

            if "gateways" in ip_config:
                # EN/JP server: gateways are direct WebSocket endpoints
                gateway_url = random.choice(ip_config["gateways"])["url"]
                # Convert https URL to wss endpoint
                endpoint = gateway_url.replace("https://", "wss://") + "/gateway"
            elif "region_urls" in ip_config:
                # CN server: region URL returns server list
                region_url = ip_config["region_urls"][0]["url"]
                async with session.get(
                    region_url + "?service=ws-gateway&protocol=ws&ssl=true"
                ) as resp:
                    server_list = await resp.json()
                if not server_list.get("servers"):
                    raise RuntimeError("No gateway servers available")
                endpoint = f"wss://{random.choice(server_list['servers'])}"
            else:
                raise RuntimeError(f"Cannot find gateway config: {ip_config}")

            logger.info("Selected gateway: %s", endpoint)

        # 5. Connect WebSocket
        self._lobby_endpoint = endpoint
        self.channel = MSRPCChannel(endpoint)
        ms_host = self.server_config["base_url"].replace("https://", "")
        await self.channel.connect(ms_host)

        self.lobby = Lobby(self.channel)
        self._connected = True
        logger.info("Connected to Majsoul successfully")

    async def _ensure_connected(self) -> None:
        """Reconnect WebSocket and re-authenticate if the connection has been dropped."""
        ws_alive = False
        if self._connected and self.channel and self.channel._ws:
            try:
                # Check if WS is still open
                ws_alive = self.channel._ws.state.name == "OPEN"
            except Exception:
                pass

        if not ws_alive:
            logger.info("WebSocket dropped, reconnecting...")
            saved_login = self._last_login
            try:
                await self.close()
            except Exception:
                pass
            await self.connect()

            # Re-authenticate using stored credentials
            if saved_login:
                method = saved_login["method"]
                logger.info("Re-authenticating after reconnect (method=%s)...", method)
                if method == "gateway":
                    await self.login_with_gateway_token(saved_login["access_token"])
                elif method == "yostar":
                    await self.login_with_yostar_token(
                        saved_login["uid"], saved_login["token"]
                    )
                logger.info("Re-authentication successful")

    def _client_version_string(self) -> str:
        """Return the version string expected by auth RPCs."""
        version = self._version or "0.11.233.w"
        if version.endswith(".w"):
            version = version[:-2]
        return f"web-{version}"

    @staticmethod
    def _format_rpc_error(error: Any) -> str:
        """Render Majsoul RPC errors with the useful server-side details."""
        if not error or not getattr(error, "code", 0):
            return "unknown error"

        parts = [f"code {error.code}"]
        json_param = getattr(error, "json_param", "") or ""
        if json_param:
            parts.append(f"json={json_param}")

        u32_params = list(getattr(error, "u32_params", []) or [])
        if u32_params:
            parts.append(f"u32={u32_params}")

        str_params = list(getattr(error, "str_params", []) or [])
        if str_params:
            parts.append(f"str={str_params}")

        return ", ".join(parts)

    async def _oauth2_auth_typed(self, code: str, uid: str, auth_type: int) -> str:
        """Exchange a Yostar/login code for a gateway access token."""
        from ms.protocol_pb2 import ReqOauth2Auth

        logger.info("oauth2Auth: type=%d, uid=%s, code=%s...", auth_type, uid, code[:12])

        req = ReqOauth2Auth()
        req.type = auth_type
        req.code = code
        req.uid = uid
        req.client_version_string = self._client_version_string()

        res = await self.lobby.oauth2_auth(req)
        if res.error and res.error.code:
            raise RuntimeError(f"oauth2Auth failed ({self._format_rpc_error(res.error)})")
        if not res.access_token:
            raise RuntimeError("oauth2Auth succeeded but did not return an access_token")

        logger.info("oauth2Auth type=%d OK", auth_type)
        return res.access_token

    async def _oauth2_check_typed(self, access_token: str, auth_type: int) -> bool:
        """Check whether an oauth2 access token belongs to an existing account."""
        from ms.protocol_pb2 import ReqOauth2Check

        req = ReqOauth2Check()
        req.type = auth_type
        req.access_token = access_token

        res = await self.lobby.oauth2_check(req)
        if res.error and res.error.code:
            raise RuntimeError(f"oauth2Check failed ({self._format_rpc_error(res.error)})")

        logger.info("oauth2Check type=%d OK (has_account=%s)", auth_type, res.has_account)
        return bool(res.has_account)

    async def login_with_gateway_token(self, access_token: str) -> dict:
        """Login using a gateway access_token directly (skip oauth2Auth).

        The gateway access_token can be obtained from browser DevTools:
          GameMgr.Inst.lq.lobby._channel._ws  (intercept WebSocket)
          or by capturing the oauth2Auth response in Network tab.

        This bypasses the oauth2Auth step entirely.
        """
        if not self._connected:
            raise RuntimeError("Not connected. Call connect() first.")

        # Try oauth2Login directly with this token
        for auth_type in OAUTH_LOGIN_TYPES:
            try:
                await self._ensure_connected()
                logger.info("Direct oauth2Login: type=%d, token=%s...", auth_type, access_token[:20])
                result = await self._login_with_access_token_typed(access_token, auth_type=auth_type)
                self._last_login = {"method": "gateway", "access_token": access_token}
                return result
            except Exception as e:
                logger.warning("Direct oauth2Login type=%d failed: %s", auth_type, e)
                continue

        raise RuntimeError("All direct oauth2Login attempts failed.")

    async def _login_with_yostar_credentials_direct(
        self,
        yostar_uid: str,
        yostar_token: str,
    ) -> dict:
        """Fallback: try the older direct `login` RPC with Yostar credentials."""
        from ms.protocol_pb2 import ReqLogin, ReqPrepareLogin

        account_variants = [yostar_uid, f"yoyo{yostar_uid}"]
        last_error = None

        for account in account_variants:
            for auth_type in DIRECT_LOGIN_TYPES:
                try:
                    # Force a fresh connection for each attempt because the
                    # server can close the socket after a failed login.
                    try:
                        await self.close()
                    except Exception:
                        pass
                    await self.connect()

                    prep_req = ReqPrepareLogin()
                    prep_req.access_token = yostar_token
                    prep_req.type = auth_type
                    prep_res = await self.lobby.prepare_login(prep_req)
                    if prep_res.error and prep_res.error.code:
                        logger.warning(
                            "prepareLogin type=%d failed: %s",
                            auth_type,
                            self._format_rpc_error(prep_res.error),
                        )
                    else:
                        logger.info("prepareLogin type=%d OK", auth_type)

                    logger.info(
                        "Direct login fallback: type=%d, account=%s, token=%s...",
                        auth_type,
                        account,
                        yostar_token[:12],
                    )

                    req = ReqLogin()
                    req.account = account
                    req.password = yostar_token
                    req.type = auth_type
                    req.reconnect = False
                    req.device.is_browser = True
                    req.device.software = "Chrome"
                    req.device.platform = "pc"
                    req.device.os = "mac"
                    req.device.sale_platform = "web"
                    req.random_key = str(random.randint(0, 2**32 - 1))
                    req.client_version_string = self._client_version_string()
                    req.gen_access_token = True
                    req.currency_platforms.append(2)
                    req.currency_platforms.append(9)

                    res = await self.lobby.login(req)
                    if res.error and res.error.code:
                        last_error = self._format_rpc_error(res.error)
                        logger.warning("Direct login type=%d failed: %s", auth_type, last_error)
                        continue

                    logger.info(
                        "Direct login fallback succeeded with type=%d. Account ID: %s",
                        auth_type,
                        res.account_id,
                    )
                    return {
                        "account_id": res.account_id,
                        "nickname": res.account.nickname if res.account else "",
                        "access_token": res.access_token or "",
                    }
                except Exception as e:
                    logger.warning("Direct login type=%d exception: %s", auth_type, e)
                    last_error = str(e)
                    continue

        raise RuntimeError(
            f"Direct login fallback failed (last error: {last_error}). "
            f"Tried account variants: {account_variants}, types: {DIRECT_LOGIN_TYPES}."
        )

    async def login_with_yostar_token(self, yostar_uid: str, yostar_token: str) -> dict:
        """Login using Yostar login credentials from the live web client.

        The credentials can be obtained from browser DevTools console after
        logging into Mahjong Soul:
          - yostar_uid:   GameMgr.Inst.yostar_login_info.LOGIN_UID
          - yostar_token: GameMgr.Inst.yostar_login_info.LOGIN_TOKEN

        The current web client still performs:
          oauth2Auth(code=LOGIN_TOKEN, uid=LOGIN_UID, client_version_string=...)
          -> oauth2Login(access_token=...)

        We try that official flow first, then fall back to direct `login(...)`
        for tokens captured from other places.
        """
        if not self._connected:
            raise RuntimeError("Not connected. Call connect() first.")

        last_error = None
        for auth_type in OAUTH_LOGIN_TYPES:
            try:
                try:
                    await self.close()
                except Exception:
                    pass
                await self.connect()

                gateway_access_token = await self._oauth2_auth_typed(
                    code=yostar_token,
                    uid=yostar_uid,
                    auth_type=auth_type,
                )
                has_account = await self._oauth2_check_typed(
                    gateway_access_token,
                    auth_type=auth_type,
                )
                if not has_account:
                    raise RuntimeError(
                        f"oauth2Check returned has_account=false for type={auth_type}"
                    )
                result = await self._login_with_access_token_typed(
                    gateway_access_token,
                    auth_type=auth_type,
                )
                self._last_login = {"method": "yostar", "uid": yostar_uid, "token": yostar_token}
                return result
            except Exception as e:
                logger.warning("oauth2Auth/oauth2Login type=%d failed: %s", auth_type, e)
                last_error = str(e)
                continue

        try:
            result = await self._login_with_yostar_credentials_direct(yostar_uid, yostar_token)
            self._last_login = {"method": "yostar", "uid": yostar_uid, "token": yostar_token}
            return result
        except Exception as e:
            direct_error = str(e)
            raise RuntimeError(
                f"All Yostar auth attempts failed. "
                f"oauth2Auth/oauth2Login last error: {last_error}. "
                f"Direct login fallback last error: {direct_error}. "
                f"Use GameMgr.Inst.yostar_login_info.LOGIN_UID and LOGIN_TOKEN, "
                f"or switch to Email Login / Gateway Token."
            ) from e

    async def login_with_passport(self, yostar_uid: str, yostar_token: str) -> dict:
        """Full auth flow via passport API (may be blocked by WAF).

        Falls back from passport → oauth2Auth → oauth2Login.
        """
        if not self._connected:
            raise RuntimeError("Not connected. Call connect() first.")

        from ms.protocol_pb2 import ReqOauth2Auth

        # Step 1: Exchange Yostar token via passport API
        passport_url = self.server_config["api_url"]
        logger.info("Step 1: Exchanging Yostar token via passport...")

        async with aiohttp.ClientSession() as session:
            payload = {"uid": yostar_uid, "token": yostar_token, "deviceId": f"web|{yostar_uid}"}
            async with session.post(passport_url, json=payload) as resp:
                data = await resp.json()

        if data.get("result") != 0:
            raise RuntimeError(
                f"Passport login failed (result={data.get('result')}). "
                f"Token may be expired or blocked by WAF."
            )

        passport_access_token = data["accessToken"]
        passport_uid = data.get("uid", yostar_uid)
        logger.info("Step 1 OK")

        # Step 2: oauth2Auth on the gateway
        logger.info("Step 2: oauth2Auth on gateway...")
        req = ReqOauth2Auth()
        req.type = 7
        req.code = passport_access_token
        req.uid = passport_uid
        req.client_version_string = self._client_version_string()

        res = await self.lobby.oauth2_auth(req)
        if res.error and res.error.code:
            raise RuntimeError(f"oauth2Auth failed ({self._format_rpc_error(res.error)})")

        gateway_access_token = res.access_token
        logger.info("Step 2 OK")

        # Step 3: oauth2Login with gateway token
        return await self._login_with_access_token_typed(gateway_access_token, auth_type=7)

    def _get_yostar_sdk_url(self) -> str:
        """Get the Yostar SDK API base URL for this server region."""
        sdk_url = self.server_config.get("yostar_sdk", "")
        if not sdk_url:
            raise RuntimeError("Yostar SDK not available for this server region (CN uses different auth).")
        return sdk_url

    def _yostar_sdk_headers(self, body_dict: dict, uid: str | None = None, token: str | None = None) -> tuple[bytes, dict]:
        """Build signed body + headers for Yostar SDK API calls.

        Returns (body_bytes, headers) so the exact signed body is sent.

        Head fields (from yostar_sdk/en/index.js.txt):
          Region, PID, Channel, Platform, Version, Lang, DeviceID, [UID], [Token], Time
        UID and Token are omitted when not logged in (mirrors JS undefined → omitted by JSON.stringify).

        Sign = MD5( JSON.stringify(head) + JSON.stringify(body) + key ).toUpperCase()
        (from yostar_sdk/en/index.js.txt GK function)
        """
        if not hasattr(self, "_device_id"):
            self._device_id = str(uuid.uuid4())

        t = int(time.time())

        # PID from game config, fallback to default
        pid = self._game_config.get("yostar_sdk_4_pid", "US-MAJONGSOUL")
        region = pid.split("-")[0]  # "US-MAJONGSOUL" → "US"

        # Build head in the same key order as the SDK JS
        head: dict[str, Any] = {
            "Region": region,
            "PID": pid,
            "Channel": "web",
            "Platform": "pc",
            "Version": "4.16.0",
            "Lang": "en",
            "DeviceID": self._device_id,
        }
        # UID and Token are only included if set (mirrors JS undefined → omitted)
        if uid:
            head["UID"] = uid
        if token:
            head["Token"] = token
        head["Time"] = t

        # Sign = MD5(head_json + body_json + key).upper()
        head_json = json_mod.dumps(head, separators=(",", ":"), ensure_ascii=False)
        body_json = json_mod.dumps(body_dict, separators=(",", ":"), ensure_ascii=False)
        sign_str = f"{head_json}{body_json}{YOSTAR_SDK_SIGNING_KEY}"
        sign = hashlib.md5(sign_str.encode("utf-8")).hexdigest().upper()

        body_bytes = body_json.encode("utf-8")

        auth = json_mod.dumps({"Head": head, "Sign": sign}, separators=(",", ":"))

        headers = {
            "Content-Type": "application/json",
            "Authorization": auth,
        }
        return body_bytes, headers

    async def _yostar_sdk_post(self, endpoint: str, payload: dict) -> dict:
        """Make a signed POST request to the Yostar SDK API.

        Ensures the body JSON used for signing matches exactly what is sent.
        """
        body_bytes, headers = self._yostar_sdk_headers(payload)
        async with aiohttp.ClientSession() as session:
            async with session.post(endpoint, data=body_bytes, headers=headers) as resp:
                data = await resp.json()
        return data

    async def send_email_code(self, email: str) -> None:
        """Send a verification code to the user's email via Yostar SDK.

        Uses the Yostar SDK API (en-sdk-api.yostarplat.com), NOT passport.mahjongsoul.com.
        """
        sdk_url = self._get_yostar_sdk_url()
        endpoint = f"{sdk_url}/yostar/send-code"
        logger.info("Sending verification code to %s via %s", email, endpoint)

        payload = {"Account": email, "Randstr": "", "Ticket": ""}
        data = await self._yostar_sdk_post(endpoint, payload)
        logger.info("Send code response: %s", data)

        if data.get("Code") != 200:
            raise RuntimeError(
                f"Failed to send code (Code={data.get('Code')}). "
                f"Make sure you use the email linked to your Majsoul account. "
                f"Response: {data}"
            )

        logger.info("Verification code sent to %s", email)

    async def login_with_email_code(self, email: str, code: str) -> dict:
        """Complete email verification and login to Majsoul.

        Flow:
        1. POST yostarplat.com/yostar/get-auth → verify code, get email_token
        2. POST yostarplat.com/user/login → exchange email_token for yostar uid+token
        3. Reconnect WebSocket (gateway may have closed idle connection)
        4. Gateway oauth2Auth + oauth2Login → complete login
        """
        sdk_url = self._get_yostar_sdk_url()

        # Step 1: Verify email code → get email_token (REST, no WS needed)
        logger.info("Step 1: Verifying code for %s...", email)
        payload = {"Account": email, "Code": code}
        data = await self._yostar_sdk_post(f"{sdk_url}/yostar/get-auth", payload)
        logger.info("get-auth response: Code=%s, keys=%s", data.get("Code"), list(data.keys()))

        if data.get("Code") != 200:
            raise RuntimeError(
                f"Code verification failed (Code={data.get('Code')}). "
                f"Check the code and try again."
            )

        email_token = data.get("Data", {}).get("Token", "")
        if not email_token:
            raise RuntimeError(f"No Token in get-auth response. Data: {data.get('Data')}")
        logger.info("Step 1 OK: got email_token")

        # Step 2: Exchange email_token for Yostar uid + token (REST, no WS needed)
        logger.info("Step 2: Exchanging email token for Yostar credentials...")
        payload = {
            "CheckAccount": 0,
            "Geetest": {"challenge": None, "seccode": None, "validate": None},
            "OpenID": email,
            "Secret": "",
            "Token": email_token,
            "Type": "yostar",
            "UserName": email,
        }
        data = await self._yostar_sdk_post(f"{sdk_url}/user/login", payload)
        logger.info("user/login response: Code=%s", data.get("Code"))
        login_data = data.get("Data", {})
        logger.info("user/login Data keys: %s", list(login_data.keys()))

        if data.get("Code") != 200:
            raise RuntimeError(f"Yostar SDK login failed (Code={data.get('Code')})")

        user_info = login_data.get("UserInfo", {})
        yostar_token = user_info.get("Token", "")
        user_id = str(user_info.get("ID", ""))
        if not user_id or not yostar_token:
            raise RuntimeError(f"No UID/Token in user/login response. UserInfo: {user_info}")
        logger.info("Step 2 OK: user_id=%s", user_id)

        # Step 3: Reconnect WebSocket (gateway closes idle connections)
        logger.info("Step 3: Reconnecting WebSocket for gateway auth...")
        await self._ensure_connected()

        # Step 4: Use the same oauth2Auth + oauth2Login flow as the web client
        logger.info("Step 4: Completing gateway auth with Yostar credentials...")
        return await self.login_with_yostar_token(user_id, yostar_token)

    async def _login_with_access_token_typed(self, access_token: str, auth_type: int = 7) -> dict:
        """Login with access token using the specified auth type."""
        from ms.protocol_pb2 import ReqOauth2Login

        logger.info("oauth2Login (type=%d)...", auth_type)
        req = ReqOauth2Login()
        req.type = auth_type
        req.access_token = access_token
        req.reconnect = False
        req.device.is_browser = True
        req.device.software = "Chrome"
        req.device.platform = "pc"
        req.device.os = "mac"
        req.device.sale_platform = "web"
        req.random_key = str(random.randint(0, 2**32 - 1))
        req.client_version_string = self._client_version_string()
        req.currency_platforms.append(2)
        req.currency_platforms.append(9)

        res = await self.lobby.oauth2_login(req)
        if res.error and res.error.code:
            raise RuntimeError(f"oauth2Login failed ({self._format_rpc_error(res.error)})")

        logger.info("Logged in successfully. Account ID: %s", res.account_id)
        return {"account_id": res.account_id, "nickname": res.account.nickname}

    async def fetch_multi_account_brief(self, account_ids: list[int]) -> dict[int, str]:
        """Fetch nicknames for multiple account IDs. Returns {account_id: nickname}."""
        from ms.protocol_pb2 import ReqMultiAccountId

        req = ReqMultiAccountId()
        req.account_id_list.extend(account_ids)

        res = await self.lobby.fetch_multi_account_brief(req)
        if res.error and res.error.code:
            raise RuntimeError(f"Failed to fetch account briefs: {res.error}")

        result = {}
        for player in res.players:
            result[player.account_id] = player.nickname
        return result

    async def fetch_contest_info(self, contest_id: int) -> dict:
        """Fetch contest details by contest ID."""
        from ms.protocol_pb2 import ReqFetchCustomizedContestByContestId

        req = ReqFetchCustomizedContestByContestId()
        req.contest_id = contest_id

        res = await self.lobby.fetch_customized_contest_by_contest_id(req)
        if res.error and res.error.code:
            raise RuntimeError(f"Failed to fetch contest: {res.error}")

        contest = res.contest_info
        logger.info("Contest: %s (unique_id=%s)", contest.contest_name, contest.unique_id)
        return {
            "unique_id": contest.unique_id,
            "contest_name": contest.contest_name,
            "contest_id": contest.contest_id,
        }

    async def fetch_contest_auth_info(self, unique_id: int) -> dict:
        """Fetch observer permission info for the logged-in account in a contest."""
        from ms.protocol_pb2 import ReqFetchCustomizedContestAuthInfo

        req = ReqFetchCustomizedContestAuthInfo()
        req.unique_id = unique_id

        res = await self.lobby.fetch_customized_contest_auth_info(req)
        if res.error and res.error.code:
            raise RuntimeError(f"Failed to fetch contest auth info: {res.error}")

        return {
            "observer_level": res.observer_level,
        }

    async def fetch_contest_game_records(self, unique_id: int, last_index: int = 0) -> list[dict]:
        """Fetch completed game records for a contest."""
        await self._ensure_connected()
        from ms.protocol_pb2 import ReqFetchCustomizedContestGameRecords

        records = []
        next_index = last_index
        seen_indexes = set()

        while True:
            if next_index in seen_indexes:
                logger.warning(
                    "Stopping contest record pagination for %s because next_index=%s repeated",
                    unique_id,
                    next_index,
                )
                break
            seen_indexes.add(next_index)

            req = ReqFetchCustomizedContestGameRecords()
            req.unique_id = unique_id
            req.last_index = next_index

            res = await self.lobby.fetch_customized_contest_game_records(req)
            if res.error and res.error.code:
                raise RuntimeError(f"Failed to fetch game records: {res.error}")

            for record in res.record_list:
                accounts_by_seat = {
                    account.seat: {
                        "account_id": account.account_id,
                        "nickname": account.nickname,
                    }
                    for account in record.accounts
                }
                ranked_players = sorted(
                    record.result.players,
                    key=lambda player: (
                        -player.part_point_1,
                        -player.total_point,
                        player.seat,
                    ),
                )
                placements_by_seat = {
                    player.seat: placement
                    for placement, player in enumerate(ranked_players, start=1)
                }

                players = []
                for result_player in record.result.players:
                    account = accounts_by_seat.get(
                        result_player.seat,
                        {"account_id": 0, "nickname": ""},
                    )
                    players.append({
                        "account_id": account["account_id"],
                        "nickname": account["nickname"],
                        "placement": placements_by_seat[result_player.seat],
                        "score": result_player.part_point_1,
                        "total_point": result_player.total_point,
                        "grading_score": result_player.grading_score,
                    })
                records.append({
                    "uuid": record.uuid,
                    "start_time": record.start_time,
                    "end_time": record.end_time,
                    "players": players,
                })

            next_page = getattr(res, "next_index", 0)
            if not next_page:
                break
            next_index = next_page

        return records

    async def fetch_live_games(self, unique_id: int) -> list[dict]:
        """Fetch currently live games in the contest."""
        await self._ensure_connected()
        from ms.protocol_pb2 import ReqFetchCustomizedContestGameLiveList

        req = ReqFetchCustomizedContestGameLiveList()
        req.unique_id = unique_id

        res = await self.lobby.fetch_customized_contest_game_live_list(req)
        if res.error and res.error.code:
            raise RuntimeError(f"Failed to fetch live games: {res.error}")

        games = []
        for game in res.live_list:
            players = []
            for player in game.players:
                players.append({
                    "account_id": player.account_id,
                    "nickname": player.nickname,
                })
            games.append({
                "uuid": game.uuid,
                "players": players,
            })

        return games

    async def fetch_game_record(self, game_uuid: str) -> dict:
        """Fetch detailed game record by UUID."""
        from ms.protocol_pb2 import ReqGameRecord

        req = ReqGameRecord()
        req.game_uuid = game_uuid

        res = await self.lobby.fetch_game_record(req)
        if res.error and res.error.code:
            raise RuntimeError(f"Failed to fetch game record: {res.error}")

        # Parse accounts and results
        accounts = {}
        for account in res.head.accounts:
            accounts[account.seat] = {
                "account_id": account.account_id,
                "nickname": account.nickname,
                "seat": account.seat,
            }

        results = []
        for i, account in enumerate(res.head.result.players):
            seat = account.seat if hasattr(account, "seat") else i
            acc_info = accounts.get(seat, {})
            results.append({
                "account_id": acc_info.get("account_id", 0),
                "nickname": acc_info.get("nickname", ""),
                "seat": seat,
                "score": account.part_point_1,
                "total_point": account.total_point,
            })

        # Sort by score descending to determine placement
        results.sort(key=lambda x: x["score"], reverse=True)
        for i, result in enumerate(results):
            result["placement"] = i + 1

        return {
            "uuid": game_uuid,
            "accounts": accounts,
            "results": results,
        }

    def add_notification_hook(self, msg_type: str, callback: Callable) -> None:
        """Register a hook for Majsoul server notifications."""
        if self.channel:
            self.channel.add_hook(msg_type, callback)

    async def enter_contest(self, unique_id: int) -> None:
        """Enter a contest lobby to receive notifications."""
        from ms.protocol_pb2 import ReqEnterCustomizedContest

        req = ReqEnterCustomizedContest()
        req.unique_id = unique_id

        res = await self.lobby.enter_customized_contest(req)
        if res.error and res.error.code:
            raise RuntimeError(f"Failed to enter contest: {res.error}")
        logger.info("Entered contest lobby")

    async def create_observe_auth(self, game_uuid: str) -> dict:
        """Get auth token to observe a live game."""
        from ms.protocol_pb2 import ReqCreateGameObserveAuth

        req = ReqCreateGameObserveAuth()
        req.game_uuid = game_uuid

        res = await self.lobby.create_game_observe_auth(req)
        if res.error and res.error.code:
            raise RuntimeError(f"Failed to create observe auth: {res.error}")

        return {
            "token": res.token,
            "location": res.location,
        }

    async def fetch_ob_token(self, game_uuid: str) -> dict:
        """Fetch an alternative observer token for a live game."""
        from ms.protocol_pb2 import ReqFetchOBToken

        req = ReqFetchOBToken()
        req.uuid = game_uuid

        res = await self.lobby.fetch_ob_token(req)
        if res.error and res.error.code:
            raise RuntimeError(f"Failed to fetch OB token: {res.error}")
        if not res.token:
            raise RuntimeError("fetchOBToken succeeded but did not return a token")

        return {
            "token": res.token,
            "delay": res.delay,
            "start_time": res.start_time,
        }

    async def get_game_server_endpoint(self) -> str:
        """Get a game server endpoint for observation."""
        async with aiohttp.ClientSession() as session:
            rand = random.randint(0, 2**31)
            base = self.server_config["base_url"]

            async with session.get(f"{base}/version.json?randv={rand}") as resp:
                version_data = await resp.json()
            version = version_data["version"]

            async with session.get(f"{base}/resversion{version}.json?randv={rand}") as resp:
                resver_data = await resp.json()

            config_prefix = resver_data["res"]["config.json"]["prefix"]
            async with session.get(f"{base}/{config_prefix}/config.json") as resp:
                config_data = await resp.json()

            ip_config = config_data["ip"][0]
            if "gateways" in ip_config:
                # EN/JP: gateway URLs are direct endpoints
                gateway_url = random.choice(ip_config["gateways"])["url"]
                return gateway_url.replace("https://", "wss://") + "/gateway"
            elif "region_urls" in ip_config:
                region_url = ip_config["region_urls"][0]["url"]
                async with session.get(
                    region_url + "?service=ws-game&protocol=ws&ssl=true"
                ) as resp:
                    server_list = await resp.json()
                if not server_list.get("servers"):
                    raise RuntimeError("No game servers available")
                return f"wss://{random.choice(server_list['servers'])}"
            else:
                raise RuntimeError("Cannot find gateway config")

    async def observe_game(self, game_uuid: str) -> "GameObserver":
        """Start observing a live game via the /ob endpoint.

        The /ob endpoint uses a text/JSON protocol for commands and streams
        binary protobuf frames for live game actions.
        """
        # 1. Get OB token via lobby
        ob_info = await self.fetch_ob_token(game_uuid)
        ob_token = ob_info["token"]
        logger.info("Got OB token for %s (delay=%ss)", game_uuid, ob_info.get("delay"))

        # 2. Build /ob URL from lobby gateway
        # self._lobby_endpoint is like "wss://engs.mahjongsoul.com/gateway"
        if self._lobby_endpoint:
            ob_url = self._lobby_endpoint.rsplit("/", 1)[0] + "/ob"
        else:
            host = self.server_config["base_url"].replace("https://", "")
            ob_url = f"wss://{host}/ob"

        logger.info("Connecting to /ob endpoint: %s", ob_url)

        # 3. Connect WebSocket with game origin.
        # Disable automatic pings — the /ob server may not respond to
        # WebSocket-level pings, which would cause the library to close
        # the connection after ping_timeout seconds.
        origin = self.server_config["base_url"]
        ws = await websockets.connect(ob_url, origin=origin, ping_interval=None)

        ob_req_id = 0

        async def send_ob_cmd(cmd, payload=None):
            nonlocal ob_req_id
            ob_req_id += 1
            msg = f"<= {cmd} {ob_req_id} {json_mod.dumps(payload or {})}"
            await ws.send(msg)
            resp = await asyncio.wait_for(ws.recv(), timeout=15)
            if isinstance(resp, str) and resp.startswith("=> "):
                stripped = resp[3:]
                space_idx = stripped.index(" ")
                return json_mod.loads(stripped[space_idx + 1:])
            raise RuntimeError(f"Unexpected /ob response: {repr(resp)[:200]}")

        try:
            # 4. Authenticate
            auth_resp = await send_ob_cmd("Auth", {"token": ob_token})

            # 5. Parse head for player info
            head_str = auth_resp.get("head", "{}")
            head = json_mod.loads(head_str) if isinstance(head_str, str) else head_str
            logger.info(
                "OB auth success for %s, players: %s",
                game_uuid,
                [p.get("nickname", "?") for p in head.get("players", [])],
            )

            # 6. Fetch past sequences (catch up on game state)
            sequences: list[str] = []
            for seq_id in range(200):
                resp = await send_ob_cmd("FetchSequence", {"id": seq_id})
                data = resp.get("data", "")
                if data:
                    sequences.append(data)
                if resp.get("seq", 0) == 0 and not data:
                    break
            logger.info("Fetched %d past sequences for %s", len(sequences), game_uuid)

            # 7. Start live observation
            await send_ob_cmd("StartOb")
            logger.info("Started /ob observation for %s", game_uuid)
        except Exception:
            try:
                await ws.close()
            except Exception:
                pass
            raise

        # 8. Create observer
        observer = GameObserver(
            game_uuid=game_uuid,
            ob_ws=ws,
            head=head,
            sequences=sequences,
        )
        return observer

    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self.channel and self.channel._ws:
            await self.channel._ws.close()
            self._connected = False
            logger.info("Disconnected from Majsoul")


class GameObserver:
    """Observes a live game via the /ob endpoint and extracts current scores."""

    def __init__(self, game_uuid: str, ob_ws, head: dict, sequences: list[str]):
        self.game_uuid = game_uuid
        self._ws = ob_ws
        self._closed = False
        self.game_ended = False
        self.final_results: list[dict] | None = None

        # Parse player info from /ob head JSON.
        # head["players"] only lists human players, each with a "seat" field.
        # head["seat_list"] lists account_ids by seat index (0 = AI).
        self.seats: dict[int, dict] = {}  # seat -> {account_id, nickname, score}
        starting_points = 25000
        try:
            starting_points = head["game_config"]["mode"]["detail_rule"]["init_point"]
        except (KeyError, TypeError):
            pass

        # Build seat map from seat_list (covers all seats including AI)
        seat_list = head.get("seat_list", [])
        for seat, account_id in enumerate(seat_list):
            self.seats[seat] = {
                "account_id": account_id,
                "nickname": f"AI-{seat}" if account_id == 0 else "",
                "score": starting_points,
            }

        # Overlay human player details
        for player in head.get("players", []):
            seat = player.get("seat", -1)
            if seat in self.seats:
                self.seats[seat]["account_id"] = player.get("account_id", 0)
                self.seats[seat]["nickname"] = player.get("nickname", "")
            elif seat >= 0:
                self.seats[seat] = {
                    "account_id": player.get("account_id", 0),
                    "nickname": player.get("nickname", ""),
                    "score": starting_points,
                }

        # Current round tracking: chang=wind (0=East,1=South), ju=hand (0-3)
        self.current_chang: int = 0
        self.current_ju: int = 0

        # Stale stream detection
        self._last_binary_frame_time: float | None = None

        # Parse past sequences to catch up on current scores
        self._parse_ob_sequences(sequences)

        # Mark initial time after sequences are loaded
        self._last_binary_frame_time = time.monotonic()

        # Start background listener for live game action frames
        self._listener_task = asyncio.create_task(self._listen_for_updates())

    def _parse_ob_sequences(self, sequences: list[str]) -> None:
        """Parse base64-encoded protobuf sequences for score data."""
        for seq_data in sequences:
            try:
                raw = base64.b64decode(seq_data)
                # Try as single Wrapper
                wrapper = Wrapper()
                wrapper.ParseFromString(raw)
                if wrapper.name:
                    self._apply_wrapper_score_update(wrapper)
            except Exception as e:
                logger.debug("Could not parse sequence data: %s", e)

    # /ob binary frames have a 14-byte header before the Wrapper protobuf:
    #   bytes[0:2]  = uint16_le sequence number
    #   bytes[2:6]  = uint32_le timestamp offset (ms)
    #   bytes[6:10] = metadata
    #   bytes[10:12]= uint16_le payload (Wrapper) length
    #   bytes[12:14]= padding
    #   bytes[14:]  = Wrapper protobuf
    _OB_FRAME_HEADER_SIZE = 14

    def _parse_ob_frame(self, data: bytes) -> Wrapper | None:
        """Parse an /ob binary frame, skipping the 14-byte header."""
        if len(data) <= self._OB_FRAME_HEADER_SIZE:
            return None
        payload = data[self._OB_FRAME_HEADER_SIZE:]
        wrapper = Wrapper()
        wrapper.ParseFromString(payload)
        return wrapper if wrapper.name else None

    _STALE_STREAM_TIMEOUT = 120  # seconds without binary frames before considering stale

    @property
    def is_alive(self) -> bool:
        """True if the observer is still connected, listening, and not stale."""
        if self._closed or self._listener_task.done():
            return False
        # Consider stale if no binary frame received for too long
        if not self.game_ended and self._last_binary_frame_time:
            elapsed = time.monotonic() - self._last_binary_frame_time
            if elapsed > self._STALE_STREAM_TIMEOUT:
                logger.info(
                    "OB stream stale for %s (no binary frame for %.0fs), marking dead",
                    self.game_uuid, elapsed,
                )
                return False
        return True

    async def _listen_for_updates(self) -> None:
        """Listen for live game action frames from /ob WebSocket."""
        keepalive_id = 1000
        msg_count = 0
        logger.info("OB listener started for %s", self.game_uuid)
        try:
            while not self._closed:
                try:
                    msg = await asyncio.wait_for(self._ws.recv(), timeout=30)
                except asyncio.TimeoutError:
                    # Send keepalive to prevent server idle-timeout (~60s)
                    try:
                        keepalive_id += 1
                        ka = f'<= FetchSequence {keepalive_id} {{"id": 0}}'
                        await self._ws.send(ka)
                    except Exception:
                        logger.info("OB keepalive failed for %s", self.game_uuid)
                        break
                    continue

                msg_count += 1
                if isinstance(msg, bytes):
                    self._last_binary_frame_time = time.monotonic()
                    try:
                        wrapper = self._parse_ob_frame(msg)
                        if wrapper:
                            event_name = wrapper.name.split(".")[-1]
                            updated = self._apply_wrapper_score_update(wrapper)
                            logger.info(
                                "OB frame #%d for %s: %s (score_updated=%s)",
                                msg_count, self.game_uuid, event_name, updated,
                            )
                        else:
                            logger.info(
                                "OB binary frame #%d for %s: %d bytes, no wrapper name",
                                msg_count, self.game_uuid, len(msg),
                            )
                    except Exception as e:
                        logger.warning(
                            "Failed to parse /ob binary frame #%d (%d bytes): %s",
                            msg_count, len(msg), e,
                        )
                elif isinstance(msg, str):
                    logger.info("OB text msg #%d for %s: %.100s", msg_count, self.game_uuid, msg)
        except websockets.exceptions.ConnectionClosed:
            if not self._closed:
                logger.info(
                    "OB connection closed for %s (received %d msgs)",
                    self.game_uuid, msg_count,
                )
        except Exception as e:
            if not self._closed:
                logger.warning(
                    "OB listener for %s ended after %d msgs: %s",
                    self.game_uuid, msg_count, e,
                )

    @property
    def current_round(self) -> str:
        """Return current round as display string, e.g. 'E1', 'S4'."""
        wind = ["E", "S", "W", "N"]
        w = wind[self.current_chang] if self.current_chang < len(wind) else f"?{self.current_chang}"
        return f"{w}{self.current_ju + 1}"

    def get_scores(self) -> dict[int, int]:
        """Get current scores mapped by account_id."""
        return {
            info["account_id"]: info["score"]
            for info in self.seats.values()
        }

    def get_final_results(self) -> list[dict] | None:
        """Return final results derived from the game stream, if available."""
        if self.final_results is None:
            return None
        return [dict(result) for result in self.final_results]

    def _apply_wrapper_score_update(self, wrapper) -> bool:
        """Apply score updates from a wrapped game action.

        Returns True when the wrapper contained a relevant score update.
        """
        event_name = wrapper.name.split(".")[-1]

        if event_name == "RecordNewRound":
            from ms.protocol_pb2 import RecordNewRound

            record = RecordNewRound()
            record.ParseFromString(wrapper.data)
            self.current_chang = record.chang
            self.current_ju = record.ju
            logger.info("NewRound for %s: chang=%d ju=%d → %s", self.game_uuid, record.chang, record.ju, self.current_round)
            if record.scores:
                self._set_scores(
                    {seat: score for seat, score in enumerate(record.scores)},
                    source=event_name,
                )
                return True
            return False

        if event_name in {"RecordHule", "ActionHule"}:
            from ms.protocol_pb2 import ActionHule

            record = ActionHule()
            record.ParseFromString(wrapper.data)
            if record.gameend and record.gameend.scores:
                self._set_final_scores(
                    {seat: score for seat, score in enumerate(record.gameend.scores)},
                    source=event_name,
                )
                return True
            if record.scores:
                self._set_scores(
                    {seat: score for seat, score in enumerate(record.scores)},
                    source=event_name,
                )
                return True
            return False

        if event_name in {"RecordHuleXueZhanEnd", "ActionHuleXueZhanEnd"}:
            from ms.protocol_pb2 import ActionHuleXueZhanEnd

            record = ActionHuleXueZhanEnd()
            record.ParseFromString(wrapper.data)
            if record.gameend and record.gameend.scores:
                self._set_final_scores(
                    {seat: score for seat, score in enumerate(record.gameend.scores)},
                    source=event_name,
                )
                return True
            if record.scores:
                self._set_scores(
                    {seat: score for seat, score in enumerate(record.scores)},
                    source=event_name,
                )
                return True
            return False

        if event_name in {"RecordNoTile", "ActionNoTile"}:
            from ms.protocol_pb2 import ActionNoTile

            record = ActionNoTile()
            record.ParseFromString(wrapper.data)
            # NoTileScoreInfo has old_scores + delta_scores (not a single .score)
            if record.scores and record.scores[0].old_scores:
                entry = record.scores[0]
                seat_scores = {
                    seat: old + delta
                    for seat, (old, delta) in enumerate(
                        zip(entry.old_scores, entry.delta_scores)
                    )
                }
                if record.gameend:
                    self._set_final_scores(seat_scores, source=event_name)
                else:
                    self._set_scores(seat_scores, source=event_name)
                return True
            return False

        if event_name in {"RecordLiuJu", "ActionLiuJu"}:
            from ms.protocol_pb2 import ActionLiuJu

            record = ActionLiuJu()
            record.ParseFromString(wrapper.data)
            if record.gameend and record.gameend.scores:
                self._set_final_scores(
                    {seat: score for seat, score in enumerate(record.gameend.scores)},
                    source=event_name,
                )
                return True
            return False

        return False

    def _set_scores(self, seat_scores: dict[int, int], *, source: str) -> None:
        changed = False
        for seat, score in seat_scores.items():
            if seat in self.seats and self.seats[seat]["score"] != score:
                self.seats[seat]["score"] = score
                changed = True

        if changed:
            logger.info(
                "%s scores for %s: %s",
                source,
                self.game_uuid,
                {s["nickname"]: s["score"] for s in self.seats.values()},
            )

    def _set_final_scores(self, seat_scores: dict[int, int], *, source: str) -> None:
        self._set_scores(seat_scores, source=source)
        self.game_ended = True
        self.final_results = self._build_final_results()
        if self.final_results is None:
            logger.warning(
                "%s final scores for %s are tied; waiting for official record for placement",
                source,
                self.game_uuid,
            )
            return
        logger.info(
            "%s final results for %s: %s",
            source,
            self.game_uuid,
            self.final_results,
        )

    def _set_final_scores_from_end_result(self, players, *, source: str) -> None:
        use_part_point = any(player.part_point_1 != 0 for player in players)
        seat_scores = {
            player.seat: (
                player.part_point_1 if use_part_point else player.total_point
            )
            for player in players
        }
        self._set_final_scores(seat_scores, source=source)

    def _build_final_results(self) -> list[dict] | None:
        ranked = sorted(
            self.seats.items(),
            key=lambda item: (-item[1]["score"], item[0]),
        )

        scores = [info["score"] for _, info in ranked]
        if len(scores) != len(set(scores)):
            return None

        placements = {
            seat: placement
            for placement, (seat, _info) in enumerate(ranked, start=1)
        }
        return [
            {
                "account_id": info["account_id"],
                "nickname": info["nickname"],
                "placement": placements[seat],
                "score": info["score"],
            }
            for seat, info in ranked
        ]

    async def close(self) -> None:
        """Stop observing and disconnect from /ob."""
        if self._closed:
            return
        self._closed = True
        if self._listener_task and not self._listener_task.done():
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        try:
            await self._ws.close()
        except Exception:
            pass
        logger.info("Stopped observing game %s", self.game_uuid)

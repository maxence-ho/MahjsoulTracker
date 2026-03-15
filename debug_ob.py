"""Debug /ob protocol - inspect head structure and binary frames."""
import asyncio
import json
import base64
import yaml
import websockets
from src.majsoul_client import MajsoulClient
from ms.protocol_pb2 import ReqFetchOBToken, Wrapper

def log(msg):
    print(msg, flush=True)

async def main():
    with open('config.yaml') as f:
        config = yaml.safe_load(f)

    client = MajsoulClient(server=config.get('server', 'en'))
    await client.connect()

    uid = config.get('yostar_uid', '')
    token = config.get('yostar_token', '')
    gw_token = await client._oauth2_auth_typed(code=token, uid=uid, auth_type=22)
    await client._login_with_access_token_typed(gw_token, auth_type=22)
    log('Logged in')

    contest_info = await client.fetch_contest_info(config.get('contest_id', 0))
    unique_id = contest_info['unique_id']
    try:
        await client.enter_contest(unique_id)
    except Exception:
        pass

    live_games = await client.fetch_live_games(unique_id)
    if not live_games:
        log('No live games')
        await client.close()
        return

    game_uuid = live_games[0]['uuid']
    log(f'Game: {game_uuid}')

    req = ReqFetchOBToken()
    req.uuid = game_uuid
    res = await client.lobby.fetch_ob_token(req)
    ob_token = res.token
    log(f'OB token: {ob_token[:30]}...')

    # Use the gateway-derived /ob URL
    ob_url = client._lobby_endpoint.rsplit("/", 1)[0] + "/ob"
    log(f'Connecting to {ob_url}...')
    ws = await websockets.connect(ob_url, origin='https://mahjongsoul.game.yo-star.com')

    req_id = 0

    async def send_cmd(cmd, payload=None):
        nonlocal req_id
        req_id += 1
        msg = f'<= {cmd} {req_id} {json.dumps(payload or {})}'
        log(f'SEND: {msg[:200]}')
        await ws.send(msg)
        resp = await asyncio.wait_for(ws.recv(), timeout=10)
        if isinstance(resp, str):
            log(f'RECV (text): {resp[:300]}')
            if resp.startswith('=> '):
                stripped = resp[3:]
                space_idx = stripped.index(' ')
                return json.loads(stripped[space_idx + 1:])
        elif isinstance(resp, bytes):
            log(f'RECV (binary): {len(resp)} bytes, first 50: {resp[:50].hex()}')
        return {}

    # Auth
    auth_resp = await send_cmd('Auth', {'token': ob_token})
    log(f'\n=== AUTH RESPONSE KEYS: {list(auth_resp.keys())} ===')

    # Full head dump
    head_raw = auth_resp.get('head', '{}')
    log(f'\n=== RAW HEAD (first 2000 chars): ===')
    log(head_raw[:2000])

    head = json.loads(head_raw) if isinstance(head_raw, str) else head_raw
    log(f'\n=== HEAD KEYS: {list(head.keys())} ===')

    # Check players / accounts structure
    players = head.get('players', [])
    accounts = head.get('accounts', [])
    log(f'\nPlayers ({len(players)}):')
    for i, p in enumerate(players):
        log(f'  [{i}] keys={list(p.keys())} -> {json.dumps(p, default=str)[:300]}')

    if accounts:
        log(f'\nAccounts ({len(accounts)}):')
        for i, a in enumerate(accounts):
            log(f'  [{i}] keys={list(a.keys())} -> {json.dumps(a, default=str)[:300]}')

    # Check result / scores
    result = head.get('result', None)
    log(f'\nResult: {result}')

    # Check config
    game_config = head.get('config', None)
    if game_config:
        log(f'\nConfig keys: {list(game_config.keys())}')

    # Fetch sequences
    log('\n=== SEQUENCES ===')
    sequences = []
    for seq_id in range(20):
        resp = await send_cmd('FetchSequence', {'id': seq_id})
        seq_val = resp.get('seq', 0)
        data = resp.get('data', '')
        log(f'Seq {seq_id}: seq={seq_val}, data_len={len(data)}')
        if data:
            sequences.append(data)
            # Try to decode as base64
            try:
                raw = base64.b64decode(data)
                log(f'  base64 decoded: {len(raw)} bytes, hex: {raw[:60].hex()}')
                # Try as Wrapper
                w = Wrapper()
                w.ParseFromString(raw)
                log(f'  Wrapper name={w.name}, data_len={len(w.data)}')
            except Exception as e:
                log(f'  decode error: {e}')
                # Maybe it's not base64 - try raw bytes
                log(f'  raw preview: {data[:100]}')
        if seq_val == 0 and not data:
            break

    # StartOb
    log('\n=== STARTOB ===')
    await send_cmd('StartOb')

    # Listen for messages
    log('\n=== LISTENING FOR MESSAGES (30s) ===')
    count = 0
    try:
        while count < 50:
            msg = await asyncio.wait_for(ws.recv(), timeout=30)
            count += 1
            if isinstance(msg, bytes):
                log(f'\n[{count}] BINARY: {len(msg)} bytes')
                log(f'  hex: {msg[:80].hex()}')
                # Try as Wrapper directly
                try:
                    w = Wrapper()
                    w.ParseFromString(msg)
                    log(f'  Wrapper name={w.name}, data_len={len(w.data)}')
                except Exception as e:
                    log(f'  Wrapper parse failed: {e}')
                # Try skipping first byte (NOTIFY-style)
                if len(msg) > 1:
                    try:
                        w = Wrapper()
                        w.ParseFromString(msg[1:])
                        if w.name:
                            log(f'  Wrapper(skip1) name={w.name}, data_len={len(w.data)}')
                    except Exception:
                        pass
                # Try skipping first 3 bytes (REQUEST/RESPONSE-style)
                if len(msg) > 3:
                    try:
                        w = Wrapper()
                        w.ParseFromString(msg[3:])
                        if w.name:
                            log(f'  Wrapper(skip3) name={w.name}, data_len={len(w.data)}')
                    except Exception:
                        pass
            elif isinstance(msg, str):
                log(f'\n[{count}] TEXT: {msg[:300]}')
    except asyncio.TimeoutError:
        log(f'\nTimeout after {count} messages')

    await ws.close()
    await client.close()
    log('\nDone')

asyncio.run(main())

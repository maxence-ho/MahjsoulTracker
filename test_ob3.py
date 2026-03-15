"""Test /ob text-JSON protocol — prove we can get live scores."""
import asyncio
import json
import yaml
import websockets
from src.majsoul_client import MajsoulClient
from ms.protocol_pb2 import ReqFetchOBToken

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
    log(f'OB token: {ob_token[:30]}..., delay={res.delay}s')

    # Connect to /ob endpoint
    ob_url = 'wss://engs.mahjongsoul.com:443/ob'
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
        log(f'RECV: {resp[:200]}')
        # Response format: "=> <req_id> <json>"
        # Strip "=> " prefix, then split on first space
        stripped = resp[3:] if resp.startswith('=> ') else resp
        space_idx = stripped.index(' ')
        resp_json = json.loads(stripped[space_idx + 1:])
        return resp_json

    # Auth
    log('\n=== Auth ===')
    auth_resp = await send_cmd('Auth', {'token': ob_token})
    log(f'Keys: {list(auth_resp.keys())}')
    log(f'create_time: {auth_resp.get("create_time")}')
    log(f'delay: {auth_resp.get("delay")}')

    # Parse head
    head = json.loads(auth_resp['head'])
    log(f'\nGame UUID: {head["uuid"]}')
    log(f'Players:')
    for p in head.get('players', []):
        log(f'  {p.get("nickname", "?")} (id={p.get("account_id", 0)})')

    # Fetch sequences
    log('\n=== Fetching sequences ===')
    for seq_id in range(20):
        resp = await send_cmd('FetchSequence', {'id': seq_id})
        seq = resp.get('seq', 0)
        data = resp.get('data', '')
        if data:
            log(f'Sequence {seq_id}: seq={seq}, data_len={len(data)}')
            # data might be base64-encoded protobuf
            log(f'  data preview: {data[:200]}')
        else:
            log(f'Sequence {seq_id}: seq={seq} (no data)')
        if seq == 0 and not data:
            break

    # StartOb
    log('\n=== StartOb ===')
    ob_resp = await send_cmd('StartOb')
    log(f'StartOb response: {ob_resp}')

    # Wait for live data
    log('\n=== Waiting for live data (10s) ===')
    try:
        while True:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            log(f'Live: {msg[:300]}')
    except asyncio.TimeoutError:
        log('No more live data')

    await ws.close()
    await client.close()

asyncio.run(main())

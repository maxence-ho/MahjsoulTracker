"""Test observation via /ob endpoint."""
import asyncio
import sys
import yaml
from src.majsoul_client import MajsoulClient, MSRPCChannel, FastTest
from ms.protocol_pb2 import ReqFetchOBToken, ReqAuthObserve, ReqCommon

def log(msg):
    print(msg, flush=True)

async def main():
    with open('config.yaml') as f:
        config = yaml.safe_load(f)

    client = MajsoulClient(server=config.get('server', 'en'))
    await client.connect()

    # Fast login - skip to type 22 directly
    uid = config.get('yostar_uid', '')
    token = config.get('yostar_token', '')

    log("Doing oauth2Auth type=22...")
    gw_token = await client._oauth2_auth_typed(code=token, uid=uid, auth_type=22)
    log("Doing oauth2Login type=22...")
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

    # Get OB token
    req = ReqFetchOBToken()
    req.uuid = game_uuid
    res = await client.lobby.fetch_ob_token(req)
    log(f'OB token: {res.token[:30]}..., delay={res.delay}s')

    # Connect to /ob endpoint!
    ob_endpoint = 'wss://engs.mahjongsoul.com:443/ob'
    log(f'Connecting to {ob_endpoint}...')
    ob_channel = MSRPCChannel(ob_endpoint)
    ms_host = client.server_config['base_url'].replace('https://', '')
    await ob_channel.connect(ms_host)
    log('Connected to /ob!')

    # authObserve on /ob
    ft = FastTest(ob_channel)
    req2 = ReqAuthObserve()
    req2.token = res.token
    log('Calling authObserve on /ob...')
    res2 = await ft.auth_observe(req2)
    if res2.error and res2.error.code:
        log(f'authObserve FAILED: code={res2.error.code}')
        await ob_channel._ws.close()
        await client.close()
        return

    log('*** authObserve SUCCESS! ***')

    # startObserve
    log('Calling startObserve...')
    res3 = await ft.start_observe(ReqCommon())
    if hasattr(res3, 'head') and res3.head:
        log(f'Game: {res3.head.uuid}')
        for p in res3.head.players:
            log(f'  {p.nickname} (id={p.account_id})')
    if hasattr(res3, 'passed') and res3.passed:
        log(f'Passed actions: {len(res3.passed.record)}')

    await ob_channel._ws.close()
    await client.close()

asyncio.run(main())

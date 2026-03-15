"""Test /ob endpoint with raw message inspection."""
import asyncio
import yaml
import websockets
from src.majsoul_client import MajsoulClient
from ms.protocol_pb2 import ReqFetchOBToken, ReqAuthObserve
from ms.base import Wrapper

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

    # Connect to /ob with raw WebSocket
    ob_url = 'wss://engs.mahjongsoul.com:443/ob'
    log(f'Connecting to {ob_url}...')
    ws = await websockets.connect(ob_url, origin='mahjongsoul.game.yo-star.com')
    log('Connected!')

    # Build authObserve request manually
    req2 = ReqAuthObserve()
    req2.token = ob_token
    msg = req2.SerializeToString()

    wrapper = Wrapper()
    wrapper.name = '.lq.FastTest.authObserve'
    wrapper.data = msg
    wrapped = wrapper.SerializeToString()

    # Send as type 2 (request) with index 1
    pkt = b'\x02' + (1).to_bytes(2, 'little') + wrapped
    log(f'Sending authObserve: {len(pkt)} bytes, hex={pkt.hex()[:100]}...')
    await ws.send(pkt)

    # Read response with timeout
    log('Waiting for response...')
    try:
        response = await asyncio.wait_for(ws.recv(), timeout=10)
        log(f'Got response! {len(response)} bytes')
        log(f'Hex: {response.hex()[:200]}')
        log(f'Type byte: {response[0]}')

        if response[0] == 3:  # RESPONSE
            idx = int.from_bytes(response[1:3], 'little')
            log(f'Response idx: {idx}')
            resp_wrapper = Wrapper()
            resp_wrapper.ParseFromString(response[3:])
            log(f'Wrapper name: {repr(resp_wrapper.name)}')
            log(f'Wrapper data hex: {resp_wrapper.data.hex()}')

            from ms.protocol_pb2 import ResCommon
            rc = ResCommon()
            rc.ParseFromString(resp_wrapper.data)
            if rc.error and rc.error.code:
                log(f'Error code: {rc.error.code}')
            else:
                log('*** SUCCESS - no error! ***')
        elif response[0] == 1:  # NOTIFY
            resp_wrapper = Wrapper()
            resp_wrapper.ParseFromString(response[1:])
            log(f'NOTIFY: name={resp_wrapper.name}, data_len={len(resp_wrapper.data)}')
        else:
            log(f'Unknown type: {response[0]}')

        # Read more messages
        for i in range(5):
            try:
                msg2 = await asyncio.wait_for(ws.recv(), timeout=3)
                log(f'Extra msg #{i+1}: type={msg2[0]}, len={len(msg2)}')
                if msg2[0] == 1:
                    w = Wrapper()
                    w.ParseFromString(msg2[1:])
                    log(f'  NOTIFY: {w.name}')
            except asyncio.TimeoutError:
                log(f'No more messages after #{i}')
                break

    except asyncio.TimeoutError:
        log('TIMEOUT - no response in 10s')

    await ws.close()
    await client.close()

asyncio.run(main())

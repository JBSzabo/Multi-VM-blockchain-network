import json
import os
import time
import threading
import requests
from datetime import datetime
from collections import deque

from flask import Flask, jsonify, request
from flask.json.provider import DefaultJSONProvider
from flask_cors import CORS
from web3 import Web3
from web3.exceptions import BlockNotFound
from web3.middleware import ExtraDataToPOAMiddleware
import logging

GETH_IPC = '/opt/ethereum/data/geth.ipc'
GETH_HTTP = 'http://127.0.0.1:8545'
MONITOR_INTERVAL = 2
MAX_HISTORY = 1000
FORK_DETECTION_DEPTH = 10
STALL_THRESHOLD = 30

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s'
)
logger = logging.getLogger(__name__)


def sanitize(obj):
    if isinstance(obj, (bytes, bytearray)):
        return '0x' + obj.hex()
    if hasattr(obj, 'items'):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, deque)):
        return [sanitize(v) for v in obj]
    if isinstance(obj, int):
        return int(obj)
    return obj


class SafeJSONProvider(DefaultJSONProvider):
    def dumps(self, obj, **kwargs):
        return json.dumps(sanitize(obj), **kwargs)

    def loads(self, s, **kwargs):
        return json.loads(s, **kwargs)


app = Flask(__name__)
app.json_provider_class = SafeJSONProvider
app.json = SafeJSONProvider(app)
CORS(app)

state = {
    'node_info': {},
    'blockchain_state': {},
    'peer_connections': [],
    'consensus_events': deque(maxlen=MAX_HISTORY),
    'fork_events': deque(maxlen=MAX_HISTORY),
    'signer_rotations': deque(maxlen=MAX_HISTORY),
    'divergence_events': deque(maxlen=MAX_HISTORY),
    'metrics': {
        'block_time': [],
        'peer_count_history': [],
        'fork_count': 0,
        'reorg_count': 0,
        'signer_rotation_count': 0,
    },
    'blocks': deque(maxlen=100),
    'last_update': None,
}

w3 = None
_w3_lock = threading.Lock()


def init_web3() -> bool:
    global w3

    with _w3_lock:
        for provider_cls, arg in [
            (Web3.IPCProvider, GETH_IPC),
            (Web3.HTTPProvider, GETH_HTTP),
        ]:
            try:
                candidate = Web3(provider_cls(arg))

                if candidate.is_connected():
                    candidate.middleware_onion.inject(
                        ExtraDataToPOAMiddleware,
                        layer=0
                    )

                    w3 = candidate

                    logger.info(
                        f"Connected via {provider_cls.__name__} — "
                        f"{w3.client_version}"
                    )

                    return True

            except Exception as e:
                logger.debug(f"{provider_cls.__name__} failed: {e}")

        logger.error("Could not connect to Geth")
        return False


def ensure_connected() -> bool:
    global w3

    try:
        if w3 and w3.is_connected():
            return True
    except Exception:
        pass

    logger.warning("Lost connection — reconnecting…")
    return init_web3()


def rpc(method: str, params: list = None):
    try:
        resp = requests.post(
            GETH_HTTP,
            json={
                'jsonrpc': '2.0',
                'id': 1,
                'method': method,
                'params': params or []
            },
            timeout=5,
        )

        data = resp.json()

        if 'error' in data:
            logger.debug(f"RPC {method} error: {data['error']}")
            return None

        return data.get('result')

    except Exception as e:
        logger.debug(f"RPC {method} exception: {e}")
        return None


def get_clique_signer(block_num: int) -> str:
    try:
        snapshot = rpc('clique_getSnapshot', [hex(block_num)])

        if snapshot:
            recents = snapshot.get('recents', {})

            for key in [str(block_num), hex(block_num)]:
                if key in recents:
                    return recents[key]

    except Exception as e:
        logger.debug(f"get_clique_signer({block_num}): {e}")

    return None


def _fetch_block_data(num: int):
    try:
        raw = w3.eth.get_block(
            num,
            full_transactions=False
        )
    except BlockNotFound:
        return None

    real_miner = get_clique_signer(num) or raw.get('miner', '')

    return {
        'number': int(raw['number']),
        'hash': raw['hash'].hex(),
        'timestamp': int(raw['timestamp']),
        'miner': real_miner,
        'transactions': len(raw.get('transactions', [])),
        'gasUsed': int(raw.get('gasUsed', 0)),
        'gasLimit': int(raw.get('gasLimit', 0)),
        'parentHash': raw['parentHash'].hex(),
    }


def get_node_info():
    try:
        info = sanitize(w3.geth.admin.node_info())

        state['node_info'] = {
            'enode': info.get('enode', ''),
            'id': info.get('id', ''),
            'name': info.get('name', ''),
            'ip': info.get('ip', ''),
            'ports': info.get('ports', {}),
            'protocols': info.get('protocols', {}),
            'timestamp': datetime.now().isoformat(),
        }

    except Exception as e:
        logger.error(f"get_node_info: {e}")


def get_blockchain_state():
    try:
        block_number = int(w3.eth.block_number)

        syncing_raw = w3.eth.syncing
        syncing = sanitize(syncing_raw) if syncing_raw else False

        gas_price = int(w3.eth.gas_price)
        mining = rpc('eth_mining') or False

        raw_block = w3.eth.get_block(
            'latest',
            full_transactions=False
        )

        block = sanitize(raw_block)

        signers_raw = rpc('clique_getSigners', ['latest']) or []
        signers = [str(s) for s in signers_raw]

        proposals_raw = rpc('clique_proposals') or {}
        proposals = {
            str(k): bool(v)
            for k, v in proposals_raw.items()
        }

        real_miner = get_clique_signer(block_number) or block.get('miner')

        state['blockchain_state'] = {
            'block_number': block_number,
            'syncing': syncing,
            'mining': mining,
            'gas_price': gas_price,
            'latest_block': {
                'number': block.get('number'),
                'hash': block.get('hash'),
                'timestamp': block.get('timestamp'),
                'miner': real_miner,
                'transactions': len(raw_block.get('transactions', [])),
                'gasUsed': block.get('gasUsed'),
                'gasLimit': block.get('gasLimit'),
                'parentHash': block.get('parentHash'),
            },
            'signers': signers,
            'signer_count': len(signers),
            'proposals': proposals,
            'timestamp': datetime.now().isoformat(),
        }

    except Exception as e:
        logger.error(f"get_blockchain_state: {e}")


def get_peer_connections():
    try:
        peer_list = []

        for raw in w3.geth.admin.peers():
            p = sanitize(raw)
            net = p.get('network', {})

            peer_list.append({
                'id': p.get('id', ''),
                'name': p.get('name', ''),
                'enode': p.get('enode', ''),
                'network': {
                    'localAddress': net.get('localAddress', ''),
                    'remoteAddress': net.get('remoteAddress', ''),
                    'inbound': bool(net.get('inbound', False)),
                    'trusted': bool(net.get('trusted', False)),
                    'static': bool(net.get('static', False)),
                },
                'protocols': p.get('protocols', {}),
                'timestamp': datetime.now().isoformat(),
            })

        state['peer_connections'] = peer_list

        history = state['metrics']['peer_count_history']

        history.append({
            'timestamp': datetime.now().isoformat(),
            'count': len(peer_list)
        })

        if len(history) > MAX_HISTORY:
            state['metrics']['peer_count_history'] = history[-MAX_HISTORY:]

    except Exception as e:
        logger.error(f"get_peer_connections: {e}")


def detect_signer_rotation():
    try:
        current = set(
            state['blockchain_state'].get('signers', [])
        )

        prev = getattr(
            detect_signer_rotation,
            '_prev',
            set()
        )

        added = current - prev
        removed = prev - current

        if (added or removed) and prev:
            event = {
                'timestamp': datetime.now().isoformat(),
                'type': 'SIGNER_ROTATION',
                'added': sorted(added),
                'removed': sorted(removed),
                'current_signers': sorted(current),
                'block_number': state['blockchain_state'].get(
                    'block_number'
                ),
            }

            state['signer_rotations'].append(event)
            state['metrics']['signer_rotation_count'] += 1

            logger.info(
                f"Signer rotation — added={added} removed={removed}"
            )

        detect_signer_rotation._prev = current

    except Exception as e:
        logger.error(f"detect_signer_rotation: {e}")


detect_signer_rotation._prev = set()


def detect_forks():
    try:
        current_num = int(w3.eth.block_number)

        if len(state['blocks']) < 2:
            return

        for i in range(
            min(
                FORK_DETECTION_DEPTH,
                len(state['blocks'])
            )
        ):
            block_num = current_num - i

            if block_num < 0:
                continue

            try:
                live_hash = w3.eth.get_block(
                    block_num
                )['hash'].hex()

            except BlockNotFound:
                continue

            cached = next(
                (
                    b for b in state['blocks']
                    if b['number'] == block_num
                ),
                None
            )

            if cached and cached['hash'] != live_hash:
                event = {
                    'timestamp': datetime.now().isoformat(),
                    'type': 'FORK_DETECTED',
                    'block_number': block_num,
                    'old_hash': cached['hash'],
                    'new_hash': live_hash,
                    'reorg_depth': i,
                    'severity': (
                        'HIGH'
                        if i > 3
                        else 'MEDIUM'
                        if i > 1
                        else 'LOW'
                    ),
                }

                state['fork_events'].append(event)
                state['metrics']['fork_count'] += 1
                state['metrics']['reorg_count'] += 1

                logger.warning(
                    f"Fork at block {block_num}, reorg depth {i}"
                )

                return

    except Exception as e:
        logger.error(f"detect_forks: {e}")


def detect_consensus_events():
    try:
        current_num = int(w3.eth.block_number)

        if state['blocks']:
            last_known = int(state['blocks'][-1]['number'])
        else:
            last_known = -1

        if last_known == -1 and current_num >= 0:
            logger.info(
                f"Backfilling blocks 0 to {current_num}..."
            )

            start = max(0, current_num - 99)

            for num in range(start, current_num + 1):
                bd = _fetch_block_data(num)

                if bd is not None:
                    state['blocks'].append(bd)

            logger.info(
                f"Backfilled {len(state['blocks'])} blocks"
            )

            last_known = (
                int(state['blocks'][-1]['number'])
                if state['blocks']
                else -1
            )

        if current_num > last_known:
            for num in range(last_known + 1, current_num + 1):
                bd = _fetch_block_data(num)

                if bd is None:
                    continue

                state['blocks'].append(bd)

                state['consensus_events'].append({
                    'timestamp': datetime.now().isoformat(),
                    'type': 'BLOCK_SEALED',
                    'block_number': bd['number'],
                    'block_hash': bd['hash'],
                    'miner': bd['miner'],
                    'transactions': bd['transactions'],
                    'gasUsed': bd['gasUsed'],
                })

                logger.info(
                    f"Block sealed #{bd['number']} by {bd['miner']}"
                )

        if state['blocks']:
            latest = list(state['blocks'])[-1]

            gap = int(time.time()) - latest['timestamp']

            if gap > STALL_THRESHOLD:
                recent_types = [
                    e.get('type')
                    for e in list(state['consensus_events'])[-5:]
                ]

                if 'CONSENSUS_STALL' not in recent_types:
                    state['consensus_events'].append({
                        'timestamp': datetime.now().isoformat(),
                        'type': 'CONSENSUS_STALL',
                        'message': f'No new blocks for {gap}s',
                        'last_block': latest['number'],
                        'severity': 'WARNING',
                    })

                    logger.warning(
                        f"Consensus stall: {gap}s since "
                        f"block #{latest['number']}"
                    )

    except Exception as e:
        logger.error(f"detect_consensus_events: {e}")


def track_block_time():
    bl = list(state['blocks'])

    if len(bl) < 2:
        return

    try:
        delta = bl[-1]['timestamp'] - bl[-2]['timestamp']

        if delta > 0:
            history = state['metrics']['block_time']

            history.append({
                'timestamp': datetime.now().isoformat(),
                'block_number': bl[-1]['number'],
                'block_time': delta,
            })

            if len(history) > MAX_HISTORY:
                state['metrics']['block_time'] = history[-MAX_HISTORY:]

    except Exception as e:
        logger.error(f"track_block_time: {e}")


def detect_chain_divergence():
    try:
        my_height = state['blockchain_state'].get('block_number')

        if my_height is None:
            return

        for peer in state['peer_connections']:
            remote = peer['network'].get('remoteAddress', '')

            if not remote:
                continue

            ip = remote.split(':')[0]
            attr = f'_div_{ip.replace(".", "_")}'

            try:
                resp = requests.post(
                    f'http://{ip}:8545',
                    json={
                        'jsonrpc': '2.0',
                        'id': 1,
                        'method': 'eth_blockNumber',
                        'params': []
                    },
                    timeout=2,
                )

                peer_height = int(
                    resp.json().get('result', '0x0'),
                    16
                )

                diff = abs(my_height - peer_height)

                if diff > 2:
                    if not getattr(
                        detect_chain_divergence,
                        attr,
                        False
                    ):
                        state['divergence_events'].append({
                            'timestamp': datetime.now().isoformat(),
                            'type': 'CHAIN_DIVERGENCE',
                            'my_height': my_height,
                            'peer_height': peer_height,
                            'difference': diff,
                            'peer_ip': ip,
                            'severity': (
                                'HIGH'
                                if diff > 10
                                else 'MEDIUM'
                            ),
                        })

                        logger.warning(
                            f"Divergence: us={my_height} "
                            f"peer={ip}:{peer_height} diff={diff}"
                        )

                    setattr(
                        detect_chain_divergence,
                        attr,
                        True
                    )

                else:
                    setattr(
                        detect_chain_divergence,
                        attr,
                        False
                    )

            except Exception:
                pass

    except Exception as e:
        logger.error(f"detect_chain_divergence: {e}")


def monitor_loop():
    logger.info("Monitoring loop started")

    while True:
        try:
            if not ensure_connected():
                time.sleep(5)
                continue

            get_node_info()
            get_blockchain_state()
            get_peer_connections()
            detect_signer_rotation()
            detect_forks()
            detect_consensus_events()
            track_block_time()
            detect_chain_divergence()

            state['last_update'] = datetime.now().isoformat()

        except Exception as e:
            logger.error(f"monitor_loop unhandled: {e}")

        time.sleep(MONITOR_INTERVAL)


def _j(obj, status=200):
    return jsonify(sanitize(obj)), status


@app.route('/health')
def health():
    return _j({
        'status': 'healthy',
        'connected': bool(w3 and w3.is_connected()),
        'timestamp': datetime.now().isoformat()
    })


@app.route('/api/status')
def api_status():
    return _j({
        'node_info': state['node_info'],
        'blockchain_state': state['blockchain_state'],
        'peer_count': len(state['peer_connections']),
        'last_update': state['last_update']
    })


@app.route('/api/node/info')
def api_node_info():
    return _j(state['node_info'])


@app.route('/api/peers')
def api_peers():
    return _j({
        'peers': state['peer_connections'],
        'count': len(state['peer_connections'])
    })


@app.route('/api/blocks')
def api_blocks():
    limit = request.args.get(
        'limit',
        default=50,
        type=int
    )

    blocks = list(state['blocks'])[-limit:]

    return _j({
        'blocks': blocks,
        'count': len(blocks)
    })


@app.route('/api/events/consensus')
def api_consensus_events():
    limit = request.args.get(
        'limit',
        default=100,
        type=int
    )

    events = list(state['consensus_events'])[-limit:]

    return _j({
        'events': events,
        'count': len(events)
    })


@app.route('/api/events/forks')
def api_fork_events():
    return _j({
        'events': list(state['fork_events']),
        'count': len(state['fork_events']),
        'total_forks': state['metrics']['fork_count']
    })


@app.route('/api/events/signers')
def api_signer_events():
    return _j({
        'events': list(state['signer_rotations']),
        'count': len(state['signer_rotations']),
        'total_rotations': state['metrics']['signer_rotation_count']
    })


@app.route('/api/events/divergence')
def api_divergence_events():
    return _j({
        'events': list(state['divergence_events']),
        'count': len(state['divergence_events'])
    })


@app.route('/api/metrics')
def api_metrics():
    bt = state['metrics']['block_time']

    avg = (
        round(
            sum(x['block_time'] for x in bt[-20:]) / len(bt[-20:]),
            2
        )
        if bt
        else 0.0
    )

    return _j({
        'average_block_time': avg,
        'fork_count': state['metrics']['fork_count'],
        'reorg_count': state['metrics']['reorg_count'],
        'signer_rotation_count': state['metrics']['signer_rotation_count'],
        'block_time_history': bt[-100:],
        'peer_count_history': state['metrics']['peer_count_history'][-100:]
    })


@app.route('/api/signers')
def api_signers():
    signers_raw = rpc('clique_getSigners', ['latest']) or []
    proposals_raw = rpc('clique_proposals') or {}

    return _j({
        'signers': [str(s) for s in signers_raw],
        'proposals': {
            str(k): bool(v)
            for k, v in proposals_raw.items()
        },
        'count': len(signers_raw)
    })


@app.route('/api/vote', methods=['POST'])
def api_vote():
    data = request.get_json(force=True, silent=True) or {}

    address = data.get('address', '').strip()
    authorize = bool(data.get('authorize', True))

    if not address:
        return _j({
            'error': 'address is required'
        }, 400)

    rpc(
        'clique_propose',
        [address, authorize]
    )

    return _j({
        'success': True,
        'address': address,
        'action': 'ADD' if authorize else 'REMOVE'
    })


@app.route('/api/transaction/send', methods=['POST'])
def api_send_tx():
    accounts = w3.eth.accounts

    if not accounts:
        return _j({
            'error': 'No unlocked accounts on this node'
        }, 400)

    data = request.get_json(
        force=True,
        silent=True
    ) or {}

    from_addr = data.get('from') or accounts[0]
    to_addr = data.get('to') or from_addr
    value = int(data.get('value', 0))

    try:
        tx_hash = w3.eth.send_transaction({
            'from': from_addr,
            'to': to_addr,
            'value': value
        })

        return _j({
            'success': True,
            'transaction_hash': tx_hash.hex()
        })

    except Exception as e:
        return _j({
            'error': str(e)
        }, 500)


if __name__ == '__main__':
    if not init_web3():
        logger.warning(
            "Initial Geth connection failed — "
            "will retry in monitor loop"
        )

    threading.Thread(
        target=monitor_loop,
        daemon=True
    ).start()

    port = int(
        os.environ.get(
            'MONITOR_PORT',
            9001
        )
    )

    logger.info(
        f"Monitoring API on 0.0.0.0:{port}"
    )

    app.run(
        host='0.0.0.0',
        port=port,
        debug=False,
        use_reloader=False
    )
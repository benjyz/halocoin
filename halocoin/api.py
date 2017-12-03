import json
import os
import threading

from flask import Flask, request, Response
from werkzeug.serving import run_simple

from halocoin import tools
from halocoin.blockchain import BlockchainService
from halocoin.service import Service


class ComplexEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (bytes, bytearray)):
            return obj.hex()
        # Let the base class default method raise the TypeError
        return json.JSONEncoder.default(self, obj)


def blockchain_synced(func):
    def wrapper(*args, **kwargs):
        if get_engine().blockchain.get_chain_state() == BlockchainService.IDLE:
            return func(*args, **kwargs)
        else:
            return 'Blockchain is syncing. This method is not reliable while operation continues.\n' + \
                   str(get_engine().db.get('length')) + '-' + str(get_engine().db.get('known_length'))

    # To keep the function name same for RPC helper
    wrapper.__name__ = func.__name__

    return wrapper


app = Flask(__name__)


@app.route('/')
def hello_world():
    return 'Hello, World!'


def get_engine():
    with app.app_context():
        return getattr(app, 'engine', None)


def shutdown_server():
    func = request.environ.get('werkzeug.server.shutdown')
    if func is None:
        raise RuntimeError('Not running with the Werkzeug Server')
    func()


def run(engine):
    with app.app_context():
        setattr(app, 'engine', engine)
    host = os.environ.get('HALOCOIN_API_HOST', "0.0.0.0")
    listen_thread = threading.Thread(target=run_simple,
                                     kwargs={'hostname': host,
                                             'port': engine.config['port']['api'],
                                             'application': app})
    listen_thread.start()
    print("Started API on {}:{}".format(host, engine.config['port']['api']))


@app.route("/upload_wallet", methods=['GET', 'POST'])
def upload_wallet():
    wallet_file = request.files['wallet_file']
    wallet_content = wallet_file.stream.read()
    index = get_engine().account.add_wallet(wallet_content)
    return generate_json_response({
        "success": True,
        "wallet_index": index
    })


@app.route('/info_wallet', methods=['GET', 'POST'])
def info_wallet():
    wallet_index = request.values.get('index', -1)
    pw = request.values.get('password', '')
    encrypted_wallet_content = get_engine().account.get_wallet(int(wallet_index))
    if encrypted_wallet_content is not None:
        try:
            wallet = tools.wallet_from_str(tools.decrypt(pw, encrypted_wallet_content).decode())
            return generate_json_response(wallet)
        except:
            return generate_json_response("Password incorrect")
    else:
        return generate_json_response("Error occurred")


@app.route('/new_wallet', methods=['GET', 'POST'])
def new_wallet():
    pw = request.values.get('password', '')
    wallet = tools.random_wallet()
    wallet_content = tools.wallet_to_str(wallet)
    wallet_encrypted_content = tools.encrypt(pw, wallet_content)
    index = get_engine().account.add_wallet(wallet_encrypted_content)
    return generate_json_response({
        "index": index,
        "success": True
    })


@app.route('/peers', methods=['GET', 'POST'])
def peers():
    return get_engine().account.get_peers()


@app.route('/node_id', methods=['GET', 'POST'])
def node_id():
    return get_engine().db.get('node_id')


@app.route('/history', methods=['GET', 'POST'])
@blockchain_synced
def history():
    address = request.values.get('address', None)
    if address is None:
        address = get_engine().db.get('address')
    account = get_engine().account.get_account(address)
    txs = {
        "send": [],
        "recv": [],
        "mine": []
    }
    for block_index in reversed(account['tx_blocks']):
        block = get_engine().db.get(str(block_index))
        for tx in block['txs']:
            tx['block'] = block_index
            owner = tools.tx_owner_address(tx)
            if owner == address:
                txs['send'].append(tx)
            elif tx['type'] == 'spend' and tx['to'] == address:
                txs['recv'].append(tx)
    for block_index in reversed(account['mined_blocks']):
        block = get_engine().db.get(str(block_index))
        for tx in block['txs']:
            tx['block'] = block_index
            owner = tools.tx_owner_address(tx)
            if owner == address:
                txs['mine'].append(tx)
    return generate_json_response(txs)


@app.route('/send', methods=['GET', 'POST'])
@blockchain_synced
def send():
    amount = request.values.get('amount', 0)
    address = request.values.get('address', None)
    message = request.values.get('message', '')
    wallet = request.values.get('wallet', None)

    if amount == 0 or address is None or wallet is None:
        return 'A problem was occurred while processing inputs'
    tx = {'type': 'spend', 'amount': int(amount),
          'to': address, 'message': message}
    wallet = tools.wallet_from_str(wallet)
    privkey, pubkey = tools.get_key_pairs_from_wallet(wallet)
    address = tools.make_address([pubkey], 1)
    if 'count' not in tx:
        try:
            tx['count'] = get_engine().account.known_tx_count(address)
        except:
            tx['count'] = 0
    if 'pubkeys' not in tx:
        tx['pubkeys'] = [pubkey.to_string()]  # We use pubkey as string
    if 'signatures' not in tx:
        tx['signatures'] = [tools.sign(tools.det_hash(tx), privkey)]
    get_engine().blockchain.tx_queue.put(tx)
    return 'Tx amount:{} to:{} added to the pool'.format(tx['amount'], tx['to'])


@app.route('/blockcount', methods=['GET', 'POST'])
def blockcount():
    result = dict(length=get_engine().db.get('length'),
                  known_length=get_engine().db.get('known_length'))
    result_text = json.dumps(result)
    return Response(response=result_text, headers={"Content-Type": "application/json"})


@app.route('/txs', methods=['GET', 'POST'])
def txs():
    return generate_json_response(get_engine().blockchain.tx_pool())


@app.route('/block', methods=['GET', 'POST'])
def block():
    number = request.values.get('number', 'default')
    if "-" in number:
        _from = int(number.split("-")[0])
        _to = int(number.split("-")[1])
        _to = min(_from + 50, _to)
        result = []
        for i in range(_from, _to):
            _block = get_engine().db.get(str(i))
            if _block is not None:
                result.append(_block)
        return generate_json_response(result)
    else:
        if number == "default":
            number = get_engine().db.get('length')
        number = int(number)
        return generate_json_response([get_engine().db.get(str(number))])


@app.route('/difficulty', methods=['GET', 'POST'])
@blockchain_synced
def difficulty():
    diff = get_engine().blockchain.target(get_engine().db.get('length'))
    return generate_json_response(diff)


@app.route('/balance', methods=['GET', 'POST'])
@blockchain_synced
def balance():
    address = request.values.get('address', None)
    if address is None:
        address = get_engine().db.get('address')
    account = get_engine().account.get_account(address, apply_tx_pool=True)
    return account['amount']


@app.route('/stop', methods=['GET', 'POST'])
def stop():
    get_engine().db.put('stop', True)
    shutdown_server()
    print('Closed API')
    get_engine().stop()
    return generate_json_response('Shutting down')


@app.route('/start_miner', methods=['GET', 'POST'])
def start_miner():
    wallet = request.values.get('wallet', None)
    if get_engine().miner.get_state() == Service.RUNNING:
        return 'Miner is already running.'
    elif wallet is None:
        return 'Given wallet is not valid.'
    else:
        wallet = tools.wallet_from_str(wallet)
        get_engine().miner.set_wallet(wallet)
        get_engine().miner.register()
        return 'Running miner'


@app.route('/stop_miner', methods=['GET', 'POST'])
def stop_miner():
    if get_engine().miner.get_state() == Service.RUNNING:
        get_engine().miner.unregister()
        return 'Closed miner'
    else:
        return 'Miner is not running.'


def generate_json_response(obj):
    result_text = json.dumps(obj, cls=ComplexEncoder)
    return Response(response=result_text, headers={"Content-Type": "application/json"})
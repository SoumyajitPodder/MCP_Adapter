"""End-to-end scenario: the sandbox's real mock backend, the lifecycle server, and the Python client,
driven through every one of the mock's drift modes. Run it with demo.sh."""
import os, sys, json, time, urllib.request
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, '..', '..', 'clients', 'python'))
from lifecycle_client import LifecycleClient, LifecycleError

BASE, MOCK = os.environ['LC_URL'], 'http://127.0.0.1:5001'
agent, ops = LifecycleClient(BASE, os.environ['LC_AGENT_TOKEN']), LifecycleClient(BASE, os.environ['LC_ADMIN_TOKEN'])
passed = failed = 0
def check(label, cond, extra=''):
    global passed, failed
    if cond: passed += 1; print('  PASS', label)
    else: failed += 1; print('  FAIL', label, extra)
def mode(m):
    req = urllib.request.Request(MOCK + '/admin/drift-mode', data=json.dumps({'mode': m}).encode(), headers={'Content-Type': 'application/json'}, method='POST')
    urllib.request.urlopen(req).read()
def blocked(tool, args):
    try: agent.call(tool, args); return None
    except LifecycleError as e: return e
def approve_all():
    out = []
    for r in ops.reviews():
        if r['kind'] == 'mapping': out.append(ops.approve(r['id'])['review'])
    return out
def health(): return {b['id']: b['health'] for b in ops.bindings()}

for _ in range(60):
    try:
        if ops.ready().get('ready') and len(ops.ready().get('pendingBindings', [])) == 0 and ops.ready()['bindings'] == 3: break
    except Exception: pass
    time.sleep(0.25)

print('1. what an agent sees')
tools = {t['name']: t for t in agent.tools()}
check('three tools, named exactly as the sandbox names them', set(tools) == {'check_device_inventory', 'get_service_ticket_status', 'get_order_status'})
check('the contract is the output schema (enum, integer, nullable optional)', tools['get_order_status']['outputSchema']['properties']['data']['items']['properties']['status']['enum'] == ['processing', 'shipped', 'delivered', 'backordered'] and tools['check_device_inventory']['outputSchema']['properties']['data']['items']['properties']['quantity_available']['type'] == 'integer')
check('arguments are constrained before anything reaches the legacy API', tools['check_device_inventory']['inputSchema']['properties']['sku']['pattern'] == '^SKU-[A-Z0-9]{1,20}$')

print('2. typed canonical answers from the real mock backend')
inv = agent.call('check_device_inventory', {'sku': 'SKU-IP15PM'})['data'][0]
check('inventory, with a real integer and a null optional', inv == {'sku': 'SKU-IP15PM', 'device_name': 'iPhone 15 Pro Max', 'quantity_available': 42, 'warehouse': 'DFW-01', 'backorder_eta': None} and type(inv['quantity_available']) is int, inv)
t3 = agent.call('get_service_ticket_status', {'ticket_id': 'TICKET-5003'})['data'][0]
check('ticket: null technician, date kept as YYYY-MM-DD', t3['technician_assigned'] is None and t3['opened_date'] == '2026-09-15', t3)
o2 = agent.call('get_order_status', {'order_id': 'ORDER-8002'})['data'][0]
check('order with three null optionals', o2['status'] == 'backordered' and o2['carrier'] is None and o2['expected_delivery'] is None and o2['quantity'] == 5, o2)

print('3. bad requests never reach the legacy API')
e = blocked('check_device_inventory', {'sku': 'SKU-NOPE'});            check('unknown SKU -> NOT_FOUND', e and e.code == 'NOT_FOUND', e)
e = blocked('check_device_inventory', {'sku': "x'; DROP TABLE users"}); check('injection attempt -> BAD_REQUEST (pattern)', e and e.code == 'BAD_REQUEST', e)
e = blocked('get_order_status', {});                                    check('missing argument -> BAD_REQUEST', e and e.code == 'BAD_REQUEST', e)
check('none of that disturbed health', set(health().values()) == {'PASS'}, health())

print("4. the sandbox's own 'rename' drift — the case its pairing logic gets wrong")
mode('rename'); ops.run_batch()
e1, e2 = blocked('check_device_inventory', {'sku': 'SKU-IP15PM'}), blocked('get_order_status', {'order_id': 'ORDER-8001'})
check('calls fail closed, naming the review that needs a person', e1 and e1.code == 'DRIFT_BLOCKED' and e1.review and e2 and e2.code == 'DRIFT_BLOCKED' and e2.review, (e1, e2))
rv = {r['bindingId']: r for r in ops.reviews() if r['kind'] == 'mapping'}
pairs = {(x['from'], x['to']) for x in rv['get_order_status']['renames']}
check('orders: BOTH the required AND the optional rename are proposed, correctly paired', pairs == {('status', 'current_status'), ('tracking_number', 'tracking_id')}, pairs)
check('inventory: quantity_available -> qty_avail', {(x['from'], x['to']) for x in rv['check_device_inventory']['renames']} == {('quantity_available', 'qty_avail')})
try: agent.approve(rv['get_order_status']['id']); check('an agent cannot approve', False)
except LifecycleError as ex: check('an agent cannot approve its own fix (403)', ex.status == 403, ex)
done = approve_all()
check('a person approves; every approval re-validated against the live upstream', len(done) >= 3 and all(r['status'] == 'approved' for r in done), [r['status'] for r in done])
o1 = agent.call('get_order_status', {'order_id': 'ORDER-8001'})['data'][0]
check('the agent is served under the stable names; the optional field FOLLOWED its rename', o1['status'] == 'shipped' and o1['tracking_number'] == '784512339981' and 'current_status' not in o1 and 'tracking_id' not in o1, o1)
check('inventory recovered', agent.call('check_device_inventory', {'sku': 'SKU-IP15PM'})['data'][0]['quantity_available'] == 42)

print('5. the upstream reverts: that is drift too')
mode('none'); ops.run_batch()
check('reverting is detected, not assumed safe', blocked('get_order_status', {'order_id': 'ORDER-8001'}).code == 'DRIFT_BLOCKED')
approve_all()
check('and recovers once confirmed', agent.call('get_order_status', {'order_id': 'ORDER-8001'})['data'][0]['status'] == 'shipped' and set(health().values()) == {'PASS'}, health())

print("6. 'enum_drift': every order's status becomes in_transit — information is destroyed")
mode('enum_drift'); ops.run_batch()
e = blocked('get_order_status', {'order_id': 'ORDER-8001'}); check('fails closed', e and e.code == 'DRIFT_BLOCKED', e)
orv = next(r for r in ops.reviews() if r['bindingId'] == 'get_order_status' and r['kind'] == 'mapping')
check('a new status value needs a person to choose', len(orv['choices']) == 1 and orv['choices'][0]['value'] == 'in_transit')
try: ops.approve(orv['id']); check('approve refused until a choice is made', False)
except LifecycleError as ex: check('approval refused until the choice is made (CHOICES_REQUIRED)', ex.code == 'CHOICES_REQUIRED', ex)
ops.choose(orv['id'], 0, 'shipped')
try: ops.approve(orv['id']); check('the gate refuses a mapping that corrupts the other records', False)
except LifecycleError as ex: check('GATE_CLOSED: mapping in_transit->shipped would corrupt backordered/delivered orders, so it is refused', ex.code == 'GATE_CLOSED' and ex.details and ex.details['shadow']['diffs'], ex)
check('so the agent keeps failing closed rather than getting wrong answers', blocked('get_order_status', {'order_id': 'ORDER-8002'}).code == 'DRIFT_BLOCKED')
for r in ops.reviews():
    if r['kind'] == 'mapping': ops.reject(r['id'])
mode('none'); ops.run_batch()
check('when the upstream recovers, so does the service', set(health().values()) == {'PASS'} and agent.call('get_order_status', {'order_id': 'ORDER-8002'})['data'][0]['status'] == 'backordered', health())

print("7. 'breaking': a required field vanishes with no replacement")
mode('breaking'); ops.run_batch()
check('orders blocked, and honestly reported as unfixable from here', health()['get_order_status'] == 'FAIL' and any(r['kind'] == 'migration' and r['stage'] == 'no_target' and r['bindingId'] == 'get_order_status' for r in ops.reviews()), health())
check('the agent is told, not guessed at', blocked('get_order_status', {'order_id': 'ORDER-8001'}).code == 'DRIFT_BLOCKED')
mode('none'); ops.run_batch()
check('it heals itself when the upstream does, and the migration card closes', set(health().values()) == {'PASS'} and not [r for r in ops.reviews() if r['kind'] == 'migration'], health())

print("8. 'date_drift': dates arrive as MM/DD/YYYY — day/month order is ambiguous, so it is not guessed")
mode('date_drift'); ops.run_batch()
e = blocked('get_service_ticket_status', {'ticket_id': 'TICKET-5001'}); check('fails closed', e and e.code == 'DRIFT_BLOCKED', e)
trv = next((r for r in ops.reviews() if r['bindingId'] == 'get_service_ticket_status' and r['kind'] == 'mapping'), None)
check('a review is opened', trv is not None)
if trv:
    try: ops.approve(trv['id']); check('approval refused', False)
    except LifecycleError as ex: check('approval is refused (GATE_CLOSED): the controller will not guess', ex.code == 'GATE_CLOSED', ex)
for r in ops.reviews():
    if r['kind'] == 'mapping': ops.reject(r['id'])
mode('none'); ops.run_batch()
check('and recovers when the upstream does', set(health().values()) == {'PASS'}, health())

print('9. every state change records WHO made it')
log = ops.audit(limit=300, action='API_REQUEST')
check('approvals and rejections are attributed to the admin token, never to the agent', any('approve' in x['msg'] and 'by ops' in x['msg'] for x in log) and not any('sandbox-agent' in x['msg'] for x in log))
print(f'\n{passed} passed, {failed} failed')
sys.exit(1 if failed else 0)

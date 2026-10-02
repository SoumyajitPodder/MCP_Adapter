"""The sandbox's own Agent -> Gateway -> MCPServer pipeline, unchanged, served through the lifecycle
controller by registering LifecycleDriver at the sandbox's EXTENSION POINT. Run it with demo.sh."""
import os, sys, json, urllib.request, io, contextlib
ROOT = os.environ['SANDBOX_ROOT']
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', '..', 'clients', 'python'))
sys.path.insert(0, ROOT)                       # the sandbox's own packages (adapter, agent, gateway, mcp, ...) win
from lifecycle_client import LifecycleClient
from agent.agent import Agent
from att_sandbox_driver import install, LifecycleDriver

agent_c, ops = LifecycleClient(os.environ['LC_URL'], os.environ['LC_AGENT_TOKEN']), LifecycleClient(os.environ['LC_URL'], os.environ['LC_ADMIN_TOKEN'])
def mode(m):
    urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:5001/admin/drift-mode', data=json.dumps({'mode': m}).encode(), headers={'Content-Type': 'application/json'}, method='POST')).read()
passed = failed = 0
def check(label, cond, extra=''):
    global passed, failed
    if cond: passed += 1; print('  PASS', label)
    else: failed += 1; print('  FAIL', label, extra)
def run(text, **kw):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):       # the sandbox narrates every hop; keep the output readable
        try: return agent.handle_task(text, **kw), None
        except Exception as e: return None, e

agent = Agent()
drv = install(agent.gateway, agent_c)
check("the sandbox's own gateway now routes the 'api' driver type to the lifecycle controller", isinstance(drv, LifecycleDriver))

print("1. the sandbox's agent pipeline, unchanged, served through the controller")
r, err = run("Is the iPhone 15 Pro Max in stock?", sku='SKU-IP15PM')
check('inventory answered', err is None and r is not None and 'iPhone' in json.dumps(r) or (r and '42' in json.dumps(r)), (r, err))
print('    agent result:', json.dumps(r)[:200])
r, err = run("Where is ORDER-8001?", order_id='ORDER-8001')
check('order answered with the canonical shape', err is None and r and 'shipped' in json.dumps(r) and '784512339981' in json.dumps(r), (r, err))

print("2. the upstream changes shape; the sandbox's agent is protected and told what is happening")
mode('rename'); ops.run_batch()
r, err = run("Where is ORDER-8001?", order_id='ORDER-8001')
text = json.dumps(r) if r else str(err)
check('no crash, no wrong answer: the agent gets a clear failure that names the pending review', ('DRIFT_BLOCKED' in text and 'review' in text) , text[:300])
print('    agent result:', text[:240])
for rv in ops.reviews():
    if rv['kind'] == 'mapping': ops.approve(rv['id'])
r, err = run("Where is ORDER-8001?", order_id='ORDER-8001')
check('after a person approves, the same agent question works again, unchanged', err is None and r and 'shipped' in json.dumps(r) and '784512339981' in json.dumps(r), (r, err))
r, err = run("Is the iPhone 15 Pro Max in stock?", sku='SKU-IP15PM')
check('and so does inventory (qty_avail followed quantity_available)', err is None and r and '42' in json.dumps(r), (r, err))
mode('none')
print(f'\n{passed} passed, {failed} failed'); sys.exit(1 if failed else 0)

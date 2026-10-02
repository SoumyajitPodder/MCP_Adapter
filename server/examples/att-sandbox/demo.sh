#!/usr/bin/env bash
# Runs the whole integration: the sandbox's mock backend, the lifecycle server (as a separate
# process, from this config), then the two demo scripts.
#
#   SANDBOX_ROOT=/path/to/mcp_sandbox bash demo.sh
#
# Needs: node 18+, python 3 with flask + requests (the sandbox's own requirements).
set -u
: "${SANDBOX_ROOT:?set SANDBOX_ROOT to your mcp_sandbox folder}"
HERE="$(cd "$(dirname "$0")" && pwd)"
export LC_AGENT_TOKEN="${LC_AGENT_TOKEN:-demo-agent-token-0123456789}" LC_ADMIN_TOKEN="${LC_ADMIN_TOKEN:-demo-admin-token-0123456789}"
export LC_URL="http://127.0.0.1:${LC_PORT:-18787}"
STATE="$(mktemp -u /tmp/lifecycle-demo-XXXXXX).json"
cleanup() { kill "${MOCK:-}" "${LC:-}" 2>/dev/null; rm -f "$STATE"; }
trap cleanup EXIT

(cd "$SANDBOX_ROOT" && exec python3 endpoint/mock_att_api.py) >/tmp/lifecycle-demo-mock.log 2>&1 & MOCK=$!
for _ in $(seq 1 60); do python3 -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:5001/health')" 2>/dev/null && break; sleep 0.25; done
node "$HERE/../../bin/lifecycle-server.js" --config "$HERE/config.json" --port "${LC_PORT:-18787}" --state "$STATE" >/tmp/lifecycle-demo-server.log 2>&1 & LC=$!
for _ in $(seq 1 60); do python3 -c "import urllib.request,json,os;d=json.load(urllib.request.urlopen(os.environ['LC_URL']+'/readyz'));exit(0 if d['ready'] else 1)" 2>/dev/null && break; sleep 0.25; done

echo "=== 1/2  end to end, through every drift mode of the sandbox's mock backend"; python3 "$HERE/e2e_demo.py"; A=$?
echo; echo "=== 2/2  the sandbox's own agent pipeline, served through the controller"; python3 "$HERE/sandbox_pipeline_demo.py" 2>&1 | grep -v "^\s*\[" ; B=${PIPESTATUS[0]}
exit $(( A || B ))

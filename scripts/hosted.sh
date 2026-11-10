#!/bin/bash
# Container entry point. If the chain service or the game server dies, the whole machine restarts: they share the
# open-hands state, so they must never come back separately. The card reader may restart on its own.
set -euo pipefail

: "${SESSION_SECRET:?set it with fly secrets}"
: "${CHAIN_API_KEY:?set it with fly secrets}"
: "${HOUSE_KEYPAIR:?set it with fly secrets}"

PORT="${PORT:-8000}"
DATA="${DATA_DIR:-/data}"
mkdir -p "$DATA/chain"
[ -f "$DATA/chain/state.json" ] || cp chain-seed/state.json "$DATA/chain/state.json"

KEY_FILE="$(mktemp)"
chmod 600 "$KEY_FILE"
printf '%s' "$HOUSE_KEYPAIR" > "$KEY_FILE"
unset HOUSE_KEYPAIR

export SERVER_KEYPAIR="$KEY_FILE" CHAIN_STATE_DIR="$DATA/chain" CHAIN_URL="http://127.0.0.1:8787" CLEAR_OPEN_HANDS_ON_BOOT=1

(cd chain/service && exec node --import tsx src/server.ts) &

for _ in $(seq 1 60); do
  python -c "import httpx; httpx.get('http://127.0.0.1:8787/health', timeout=1).raise_for_status()" 2>/dev/null && break
  sleep 1
done

python -m uvicorn server.app:app --host 0.0.0.0 --port "$PORT" --no-access-log &

(while true; do python -m vision.worker --server "http://127.0.0.1:$PORT" || true; sleep 2; done) &

trap 'kill $(jobs -p) 2>/dev/null; exit 0' TERM INT    # a deploy or autostop: stop the services cleanly

wait -n
echo "a core service stopped; restarting the machine" >&2
exit 1

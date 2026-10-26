#!/bin/sh
# Runs Match Intelligence on an NVIDIA Jetson (Orin Nano, JetPack with nvidia-container-toolkit)
# as the club's match device, from this machine over SSH:
#
#   1. This working tree to ~/bh-albion: the loops, their overlay and what post sends for each
#      source (as vm/push.sh); each layer's .env the first time only (yours, mode 600), with
#      the links in the emails on the Jetson's address
#   2. The iot, ai and extras layers under the Jetson's overrides (iot.yml: the shared p4n4-net;
#      ai.yml: Ollama on the GPU; the team of agents on it, without Azure OpenAI), then the
#      footage's matches and plays (clips.py post)
#   3. p4n4-api from a p4n4 checkout's lib and api (P4N4_SRC), as scripts/dev runs it, on the
#      Jetson's 127.0.0.1:8000: reach it with an SSH tunnel (below), the dev dashboard's proxy
#      expects it on localhost:8000
#
#   deploy/jetson/up.sh raisga@192.168.100.2
#   P4N4_SRC=~/src/p4n4 deploy/jetson/up.sh <user>@<host>      (default: ../p4n4 beside this repo)
#   ssh -N -L 8000:127.0.0.1:8000 <user>@<host>                then the dashboard (scripts/dev
#                                                              dashboard), its host set to <host>
#
# The board's 8 GB are shared by CPU and GPU: stop whatever else runs there first (Ollama alone
# takes 4.6 GB for gemma4:e2b).
set -eu
cd "$(dirname "$0")/../.."
target=${1:?usage: deploy/jetson/up.sh <user>@<host>}
host=${target#*@}
src=${P4N4_SRC:-../p4n4}
[ -f "$src/scripts/dev_api.py" ] || { echo "no p4n4 checkout at $src (P4N4_SRC)" >&2; exit 1; }
say() { printf '\n== %s %s\n' "$(date +%H:%M:%S)" "$*"; }

say "Project to $target:bh-albion"
rsync -az --exclude .git --exclude '.env' --exclude 'iot/data/ingest/*' --exclude 'extras/clips/*' \
    --exclude 'ai/vision/models/' --exclude 'ai/vision/shirts/real/' --exclude 'ai/vision/shirts/outputs/' \
    --exclude .cache --exclude .p4n4-api --exclude static-demo --exclude __pycache__ \
    --exclude .terraform --exclude 'terraform.tfstate*' ./ "$target:bh-albion/"
for layer in iot ai extras; do
    rsync -a --ignore-existing --chmod=F600 "$layer/.env" "$target:bh-albion/$layer/.env"
done
clips=$(python3 -c 'import json; print(" ".join(json.load(open("extras/config/clips.json"))["sources"]))')
ssh "$target" mkdir -p bh-albion/extras/clips/analysis
if ls extras/clips/*.mp4 >/dev/null 2>&1; then
    rsync -az extras/clips/*.mp4 extras/clips/vision.json "$target:bh-albion/extras/clips/"
fi
for s in $clips; do
    [ -f "extras/clips/analysis/$s/alerts.json" ] || continue
    rsync -az --delete --include alerts.json --include summary.json --include 'keyframes/***' --exclude '*' \
        "extras/clips/analysis/$s/" "$target:bh-albion/extras/clips/analysis/$s/"
done

say "p4n4 lib and api from $src"
ssh "$target" mkdir -p p4n4-src/scripts
rsync -az --delete --exclude __pycache__ --exclude .venv --exclude tests --exclude '*.egg-info' \
    "$src/lib" "$src/api" "$target:p4n4-src/"
rsync -az "$src/scripts/dev_api.py" "$target:p4n4-src/scripts/"

# shellcheck disable=SC2087  # $host expands here, the rest on the Jetson
ssh "$target" sh -eu <<EOF
cd bh-albion
sed -i "s#^ANALYSIS_PUBLIC_URL=.*#ANALYSIS_PUBLIC_URL=http://$host:8090/analysis/#" iot/.env
sed -i -e "s#^ANALYSIS_URL=.*#ANALYSIS_URL=http://$host:8090/analysis/#" -e "s#^N8N_HOST=.*#N8N_HOST=$host#" ai/.env
# Without Azure OpenAI, the team of agents on the Jetson's own model (team.py, AGENT_BACKEND=local)
if ! grep -q '^AZURE_OPENAI_ENDPOINT=.' ai/.env; then
    sed -i '/^AGENT_BACKEND=/d' ai/.env && echo AGENT_BACKEND=local >> ai/.env
fi

echo "== stacks"
docker network inspect p4n4-net >/dev/null 2>&1 || docker network create --subnet 172.20.0.0/16 p4n4-net >/dev/null
for layer in iot ai extras; do
    over=""; [ -f deploy/jetson/\$layer.yml ] && over="-f deploy/jetson/\$layer.yml"
    docker compose -p \$layer --project-directory \$layer -f \$layer/docker-compose.yml \$over up -d --build --remove-orphans
done
if ls extras/clips/analysis/*/alerts.json >/dev/null 2>&1; then
    for _ in \$(seq 60); do
        [ "\$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8090/)" != 000 ] && break
        sleep 2
    done
    python3 extras/scripts/clips.py post || echo "clips.py post failed: the footage's plays aren't in the feed" >&2
fi

echo "== p4n4-api on 127.0.0.1:8000"
cd ~/p4n4-src
[ -x .venv/bin/p4n4-api ] || python3 -m venv .venv
.venv/bin/pip install -q -e ./lib -e ./api
pkill -f 'p4n4-src/scripts/dev_api.py' || true
mkdir -p ~/bh-albion/.p4n4-api
P4N4_PROJECT_DIR=\$HOME/bh-albion P4N4_API_DATA_DIR=\$HOME/bh-albion/.p4n4-api P4N4_API_DEV_USERS=true \
    P4N4_API_PORT=8000 nohup .venv/bin/python \$HOME/p4n4-src/scripts/dev_api.py \
    < /dev/null > ~/bh-albion/.p4n4-api/dev-api.log 2>&1 &
for _ in \$(seq 60); do curl -fsS -o /dev/null http://127.0.0.1:8000/health 2>/dev/null && break; sleep 1; done
curl -fsS -o /dev/null http://127.0.0.1:8000/health && echo "p4n4-api up" || { tail -20 ~/bh-albion/.p4n4-api/dev-api.log; exit 1; }
EOF

say "Up on $host"
cat <<EOF
  Cameras        http://$host:1985/tv.mp4 …          Grafana  http://$host:3000/
  Analysis room  http://$host:8090/analysis/         Inbox    http://$host:8025/
  Dashboard      ssh -N -L 8000:127.0.0.1:8000 $target, then the dev dashboard with its host set to $host
EOF

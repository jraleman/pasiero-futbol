#!/bin/sh
# Copies this working tree to the VM and deploys it, while the repository isn't public (or to
# try a change before pushing it). Without repo_url, the VM waits for this after first boot.
#
#   deploy/azure/vm/push.sh [p4n4@<ip>]      (default: terraform output connect)
set -eu
cd "$(dirname "$0")/../../.."
target=${1:-$(terraform -chdir=deploy/azure output -raw connect | sed 's/^ssh //')}
rsync -az --delete --exclude .git --exclude '.env' --exclude 'iot/data/ingest/*' --exclude 'extras/clips/*' \
    --exclude 'ai/vision/models/' --exclude .cache --exclude .p4n4-api --exclude static-demo --exclude __pycache__ \
    --exclude .terraform --exclude 'terraform.tfstate*' ./ "$target:bh-albion/"
# The footage's cameras: the loops, their overlay, and what post sends for each source in the
# manifest (its plays and their frames); not the sources themselves, nor any other analysis
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
ssh "$target" bh-deploy

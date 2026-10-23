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
ssh "$target" bh-deploy

#!/bin/sh
# Deploys Match Intelligence on the Azure VM, as the p4n4 user: the first time (cloud-init), and
# every time after (bh-deploy, which pulls the branch first). Idempotent.
#
#   1. The secrets from Key Vault, with the VM's managed identity (no key on the disk)
#   2. Each layer's .env: its .env.example, the secrets, the public addresses, Azure OpenAI
#      by Entra ID (no key)
#   3. The project's localhost links made public (HOME.md, the theme, the cameras)
#   4. The stacks under p4n4-emu's nuc profile: the club's device, 4 cores and 14 GB
#   5. The dashboard's image in the project's theme, then the front: Caddy, the dashboard,
#      p4n4-api, and the judge's and admin's logins
#
# /etc/match-intelligence.env (cloud-init, from Terraform) says where: FQDN, KEY_VAULT,
# AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT, DASHBOARD_REF, P4N4_API_VERSION.
set -eu
. /etc/match-intelligence.env
export PATH="$HOME/.local/bin:$PATH"
REPO=$(cd "$(dirname "$0")/../../.." && pwd)
HERE="$REPO/deploy/azure/vm"
PUBLIC="https://$FQDN:8443"
say() { printf '\n== %s %s\n' "$(date -u +%H:%M:%S)" "$*"; }

# ── 1. Secrets ─────────────────────────────────────────────────────────────────
say "Key Vault $KEY_VAULT"
imds="http://169.254.169.254/metadata/identity/oauth2/token?api-version=2018-02-01&resource=https%3A%2F%2Fvault.azure.net"
token=
# A new VM's role on the vault takes a few minutes to arrive
for _ in $(seq 60); do
    token=$(curl -sf -H Metadata:true "$imds" | jq -r .access_token) &&
        curl -sf -o /dev/null -H "Authorization: Bearer $token" \
            "https://$KEY_VAULT.vault.azure.net/secrets/judge-password?api-version=7.4" && break
    token=
    sleep 10
done
[ -n "$token" ] || { echo "Key Vault $KEY_VAULT unreachable with the VM's identity" >&2; exit 1; }
secret() {
    curl -sf -H "Authorization: Bearer $token" "https://$KEY_VAULT.vault.azure.net/secrets/$1?api-version=7.4" | jq -re .value
}

# ── 2. The layers' .env ────────────────────────────────────────────────────────
say "Settings"
# KEY=value over the example's line (or added): the file is made again on every deploy
put() {
    if grep -q "^$2=" "$1"; then
        sed -i "s|^$2=.*|$2=$3|" "$1"
    else
        printf '%s=%s\n' "$2" "$3" >> "$1"
    fi
}
for layer in iot ai extras; do
    install -m 600 "$REPO/$layer/.env.example" "$REPO/$layer/.env"
done
# One assignment each, so a missing secret stops the deploy (set -e)
read_token=$(secret ingest-read-token)
site_token=$(secret site-token)
notify_token=$(secret notify-token)
influx_password=$(secret influxdb-password)
influx_token=$(secret influxdb-token)
grafana_password=$(secret grafana-password)
n8n_password=$(secret n8n-owner-password)
n8n_key=$(secret n8n-encryption-key)
judge=$(secret judge-password)
admin=$(secret admin-password)

f="$REPO/iot/.env"
put "$f" SITE_TOKEN "$site_token"
put "$f" INGEST_READ_TOKEN "$read_token"
put "$f" NOTIFY_TOKEN "$notify_token"
put "$f" INFLUXDB_PASSWORD "$influx_password"
put "$f" INFLUXDB_TOKEN "$influx_token"
put "$f" GRAFANA_PASSWORD "$grafana_password"
put "$f" ANALYSIS_PUBLIC_URL "$PUBLIC/analysis/"
put "$f" GRAFANA_ANONYMOUS true
put "$f" GRAFANA_ALLOW_EMBEDDING true
put "$f" GRAFANA_SUB_PATH /grafana/
put "$f" GRAFANA_ROOT_URL "https://$FQDN/grafana/"

f="$REPO/ai/.env"
put "$f" INGEST_READ_TOKEN "$read_token"
put "$f" NOTIFY_TOKEN "$notify_token"
put "$f" INFLUXDB_TOKEN "$influx_token"
put "$f" N8N_OWNER_PASSWORD "$n8n_password"
put "$f" N8N_ENCRYPTION_KEY "$n8n_key"
put "$f" ANALYSIS_URL "$PUBLIC/analysis/"
put "$f" AGENT_BACKEND auto
put "$f" AZURE_OPENAI_ENDPOINT "$AZURE_OPENAI_ENDPOINT"
put "$f" AZURE_OPENAI_DEPLOYMENT "$AZURE_OPENAI_DEPLOYMENT"
put "$f" AZURE_OPENAI_API_KEY ""
put "$f" MAILPIT_WEBROOT /mail/
put "$f" MAILPIT_UI_AUTH "judge:$judge"

# ── 3. Public links ────────────────────────────────────────────────────────────
say "Links on $FQDN"
for f in "$REPO/HOME.md" "$REPO/theme/brand.json"; do
    sed -i -e "s#http://localhost:8090/#$PUBLIC/#g" -e "s#http://localhost:8025/\{0,1\}\"#$PUBLIC/mail/\"#g" \
        -e "s#http://localhost:8025/\{0,1\})#$PUBLIC/mail/)#g" "$f"
done
jq --arg host "$FQDN" '.defaults.host = $host' "$REPO/theme/brand.json" > /tmp/brand.json && mv /tmp/brand.json "$REPO/theme/brand.json"
# The cameras through Caddy (https), not go2rtc's own port; the feed stays on localhost (p4n4-api)
jq --arg base "$PUBLIC/cameras" \
    '.dashboard.cameras |= map(if .port then {id, name, url: ($base + .path)} else . end)' \
    "$REPO/.p4n4.json" > /tmp/p4n4.json && mv /tmp/p4n4.json "$REPO/.p4n4.json"

# ── 4. The stacks, on the club's device ────────────────────────────────────────
say "Stacks (p4n4-emu, nuc)"
cd "$REPO"
p4n4-emu up --profile nuc --build

# ── 5. The front ───────────────────────────────────────────────────────────────
say "Dashboard image"
D="$HOME/p4n4-dashboard"
[ -d "$D/.git" ] || git clone -q https://github.com/raisga/p4n4-dashboard "$D"
git -C "$D" fetch -q origin "$DASHBOARD_REF"
git -C "$D" checkout -q --detach FETCH_HEAD
brand=$(jq -r .id "$REPO/theme/brand.json")
rm -rf "$D/brands/$brand"
cp -r "$REPO/theme" "$D/brands/$brand"
docker build -q --build-arg BRAND="$brand" -t "p4n4-dashboard:$brand" "$D"

say "Front (Caddy, dashboard, p4n4-api)"
mkdir -p -m 700 "$HOME/p4n4-api-data"
export SITE="$FQDN" DASHBOARD_IMAGE="p4n4-dashboard:$brand" P4N4_PROJECT_DIR="$REPO" P4N4_API_VERSION \
    P4N4_API_DATA="$HOME/p4n4-api-data" P4N4_UID="$(id -u)" P4N4_GID="$(id -g)"
docker compose -f "$HERE/compose.yml" -p front up -d --remove-orphans

# The logins: created once (changing a password would sign them out)
for _ in $(seq 30); do docker exec p4n4-api p4n4-api users list >/dev/null 2>&1 && break; sleep 2; done
users=$(docker exec p4n4-api p4n4-api users list | awk '{print $1}')
add_user() {
    printf '%s\n' "$users" | grep -qx "$1" ||
        printf '%s\n' "$3" | docker exec -i p4n4-api p4n4-api users add "$1" --role "$2" --password-stdin
}
add_user admin admin "$admin"
add_user judge operator "$judge"

say "Up"
cat <<EOF
  Dashboard      https://$FQDN/                  judge / (terraform output judge_login)
  Analysis room  $PUBLIC/analysis/       any name / the staff token
  Inbox          $PUBLIC/mail/           judge / the same password
  Agents         Azure OpenAI $AZURE_OPENAI_DEPLOYMENT, by the VM's managed identity
EOF

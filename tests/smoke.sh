#!/usr/bin/env bash
# ==============================================================================
# End-to-end smoke test: starts the iot and extras layers in a throwaway copy, as
# separate Compose projects the way `p4n4 up` does, posts the demo season's past
# matches and the live match so far, and checks the agent API, the game plans,
# the feed and a call through it (as p4n4-api and the analysis room make them),
# InfluxDB, MQTT, every Grafana panel, the analysis room's pages, and the cameras
# the manifest lists. From the ai layer it starts n8n and Mailpit (not the model):
# a key deviation reaches the staff's inbox, a normal one doesn't, and the
# half-time and match reports go out. The agent tools are covered offline by
# tests/test_local_tools.py. Tears everything down (including volumes) on exit.
#
#   ./tests/smoke.sh            # from the project directory
#   KEEP=1 ./tests/smoke.sh     # leave the stacks running for inspection
#
# Requires Docker with Compose v2 and python3. The test stacks drop the fixed
# container names, host ports and network name, so they run alongside other stacks.
# ==============================================================================
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK_DIR="$(mktemp -d)"
# The project without its footage, the dataset and its cache
tar -C "$PROJECT_DIR" --exclude='./extras/clips/*' --exclude='./.cache' -cf - . | tar -C "$WORK_DIR" -xf -
rm -rf "$WORK_DIR"/iot/data/ingest/ingest.db* "$WORK_DIR"/iot/data/ingest/evidence "$WORK_DIR"/iot/data/ingest/backup-*
cd "$WORK_DIR/iot"

SITE=brighton
SITE_TOKEN=smoke-site-token
READ_TOKEN=smoke-read-token
NETWORK=p4n4-brighton-smoke-net
# Deterministic data: no live simulator; `simulate.py --once` posts the history and the live match so far
sed -e 's/^COMPOSE_PROFILES=.*/COMPOSE_PROFILES=/' \
    -e "s/^DATA_UID=.*/DATA_UID=$(id -u)/" \
    -e "s/^DATA_GID=.*/DATA_GID=$(id -g)/" \
    -e "s/^SITE_ID=.*/SITE_ID=$SITE/" \
    -e "s/^SITE_TOKEN=.*/SITE_TOKEN=$SITE_TOKEN/" \
    -e "s/^EXTRA_SITE_TOKENS=.*/EXTRA_SITE_TOKENS=other-club:other-club-token/" \
    -e "s/^INGEST_READ_TOKEN=.*/INGEST_READ_TOKEN=$READ_TOKEN/" \
    -e 's/^SIMULATOR_HISTORY=.*/SIMULATOR_HISTORY=3/' \
    .env.example > .env
env_value() { grep -E "^$1=" .env | cut -d= -f2-; }

cat > docker-compose.override.yml <<EOF
services:
  ingest:
    container_name: !reset null
    ports: !reset []
  mqtt:
    container_name: !reset null
    ports: !reset []
  influxdb:
    container_name: !reset null
    ports: !reset []
  grafana:
    container_name: !reset null
    ports: !reset []
  simulator:
    container_name: !reset null
networks:
  p4n4-net:
    name: $NETWORK
    ipam: !reset {}
EOF

# The extras layer: the TV camera stands in as an HTTP MJPEG URL (the tactical
# camera's own stream; its ?, = and & exercise video.sh's quoting), the real
# footage cameras loop test clips from extras/clips/ (p4n4-clips serves them too), the tactical one is synthetic
sed -e 's|^CAMERA_TV_URL=.*|CAMERA_TV_URL=http://localhost:1984/api/stream.mjpeg?src=tactical\&smoke=1|' \
    ../extras/.env.example > ../extras/.env
cat > ../extras/docker-compose.override.yml <<EOF
services:
  video:
    container_name: !reset null
    ports: !reset []
  clips:
    container_name: !reset null
    ports: !reset []
networks:
  p4n4-net:
    name: $NETWORK
EOF

# The ai layer: n8n and Mailpit only, the reports two seconds after the whistles
sed -e "s/^INGEST_READ_TOKEN=.*/INGEST_READ_TOKEN=$READ_TOKEN/" -e 's/^REPORT_DELAY=.*/REPORT_DELAY=2/' \
    -e 's/^HALFTIME_DELAY=.*/HALFTIME_DELAY=2/' ../ai/.env.example > ../ai/.env
cat > ../ai/docker-compose.override.yml <<EOF
services:
  n8n:
    container_name: !reset null
    ports: !reset []
  mailpit:
    container_name: !reset null
    ports: !reset []
  agent:
    container_name: !reset null
    ports: !reset []
  ollama:
    container_name: !reset null
networks:
  p4n4-net:
    name: $NETWORK
EOF

INFLUXDB_ORG="$(env_value INFLUXDB_ORG)"
INFLUXDB_BUCKET="$(env_value INFLUXDB_BUCKET)"
GRAFANA_USER="$(env_value GRAFANA_USER)"
GRAFANA_PASSWORD="$(env_value GRAFANA_PASSWORD)"

PROJECT=p4n4-brighton-smoke
compose() { docker compose --project-name "$PROJECT" "$@"; }
compose_extras() { docker compose --project-directory ../extras --project-name "$PROJECT-extras" "$@"; }
compose_ai() { docker compose --project-directory ../ai --project-name "$PROJECT-ai" "$@"; }

cleanup() {
  status=$?
  if [ "${KEEP:-0}" = 1 ]; then
    echo "KEEP=1: stacks left running in $WORK_DIR (projects $PROJECT, $PROJECT-ai, $PROJECT-extras)"
  else
    compose_extras down -v --remove-orphans >/dev/null 2>&1 || true
    compose_ai down -v --remove-orphans >/dev/null 2>&1 || true
    compose down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$WORK_DIR"
  fi
  exit $status
}
trap cleanup EXIT

failed=0
ok() { echo "  ok   $1"; }
fail() { echo "  FAIL $1"; failed=1; }

# An HTTP request to the ingest service from inside its own container:
#   ingest METHOD PATH [TOKEN] [JSON]  → prints "<status> <body>"
ingest() {
  compose exec -T ingest python - "$@" <<'PY'
import sys, urllib.request, urllib.error
method, path = sys.argv[1], sys.argv[2]
token = sys.argv[3] if len(sys.argv) > 3 else ""
body = sys.argv[4].encode() if len(sys.argv) > 4 else None
req = urllib.request.Request("http://localhost:8080" + path, data=body, method=method,
                             headers={"Content-Type": "application/json", **({"Authorization": "Bearer " + token} if token else {})})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        print(r.status, r.read().decode())
except urllib.error.HTTPError as e:
    print(e.code, e.read().decode())
PY
}
field() { python3 -c "import json,sys; d=json.loads(sys.stdin.read().split(' ',1)[1]); print(eval(sys.argv[1], {}, {'d': d}))" "$1"; }
smoke_match() { compose --profile demo run --rm --no-deps -T simulator python - "$1" "$2" < "$WORK_DIR/tests/smoke_match.py" >/dev/null; }

echo "== starting the iot layer (project $PROJECT)"
compose up -d --wait --quiet-pull >/dev/null 2>&1 || { compose logs --no-color --tail 30; exit 1; }

echo "== posting the club's default plan, the past matches with their plans and calls, the live match so far (simulate.py --once)"
compose exec -T mqtt sh -c "mosquitto_sub -h localhost -t 'brighton/$SITE/#' -C 60 -W 180" > "$WORK_DIR/mqtt.txt" &
mqtt_pid=$!
sleep 1
compose --profile demo run --rm --no-deps simulator python -u /scripts/simulate.py --once 2>&1 | tail -2

echo "== agent API"
summary="$(ingest GET /api/v1/agent/match "$READ_TOKEN")"
[ "$(echo "$summary" | field 'd["match"]["site_id"]')" = "$SITE" ] && ok "the current match: $(echo "$summary" | field 'd["match"]["match_id"]')" \
  || fail "match summary: ${summary:0:300}"
[ "$(echo "$summary" | field 'd["synthetic_data"]')" = True ] && ok "demo data is marked synthetic" || fail "synthetic_data: ${summary:0:300}"
[ "$(echo "$summary" | field 'len(d["game_plan"]["targets"])')" -ge 6 ] && ok "the live match is checked against its game plan" || fail "game plan: ${summary:0:300}"
season="$(ingest GET '/api/v1/agent/season?period=60d' "$READ_TOKEN")"
played="$(echo "$season" | field 'd["record"]["played"]')"
[ "$played" -ge 3 ] && ok "the season: $played matches played ($(echo "$season" | field 'd["record"]["points"]') points)" || fail "season: ${season:0:300}"
# The latest past match, never the live one (it can be at full time, its deviations not called yet)
CURRENT="$(echo "$summary" | field 'd["match"]["match_id"]')"
PAST="$(echo "$season" | field '[m for m in d["matches"] if m["status"] == "full_time" and m["match_id"] != "'"$CURRENT"'"][-1]["match_id"]')"
r="$(ingest GET "/api/v1/agent/plan?match_id=$PAST" "$READ_TOKEN")"
[ "$(echo "$r" | field 'all(len(t["windows"]) == 18 for t in d["targets"])')" = True ] && ok "a past match's plan, target by target, every 5 minutes" || fail "plan: ${r:0:300}"
r="$(ingest GET "/api/v1/agent/formation?match_id=$PAST" "$READ_TOKEN")"
[ "$(echo "$r" | field 'd["teams"]["BHA"]["match"]["out_of_possession"]["formation"]')" != None ] && ok "the shapes read, both teams" || fail "formation: ${r:0:300}"
play="$(ingest GET "/api/v1/agent/plays?match_id=$PAST&type=goal" "$READ_TOKEN" | field 'd["plays"][0]["play_id"] if d["plays"] else ""')"
[ -n "$play" ] && ok "the past match's goals are plays" || fail "no goal play in $PAST"
r="$(ingest GET "/api/v1/analysis/play?play_id=$play" "$READ_TOKEN")"
[ "$(echo "$r" | field 'len(d["track"]["frames"]) > 20 and d["keyframe"]')" = True ] && ok "a play, with its replay and keyframe" || fail "play: ${r:0:300}"
r="$(ingest GET /api/v1/agent/nodes "$READ_TOKEN")"
[ "$(echo "$r" | field 'd["nodes"][0]["online"]')" = True ] && ok "the match device is online" || fail "nodes: $r"
r="$(ingest GET "/api/v1/agent/set_pieces?match_id=$PAST" "$READ_TOKEN")"
[ "$(echo "$r" | field 'd["teams"][[k for k in d["teams"] if d["teams"][k]["ours"]][0]]["deliveries"] > 0 and all(p["first_contact"] for p in d["set_pieces"])')" = True ] \
  && ok "a past match's set pieces, each with its first contact" || fail "set pieces: ${r:0:300}"
r="$(ingest GET "/api/v1/agent/physical?match_id=$PAST" "$READ_TOKEN")"
[ "$(echo "$r" | field 'len([p for t in d["teams"].values() if t["ours"] for p in t["players"]]) >= 14')" = True ] \
  && ok "every player's running, the substitutes too" || fail "running: ${r:0:300}"
r="$(ingest GET '/api/v1/analysis/set_pieces?period=60d' "$READ_TOKEN")"
[ "$(echo "$r" | field 'd["for"]["deliveries"] > 0 and len(d["matches"]) >= 3')" = True ] && ok "the season's set pieces" || fail "season set pieces: ${r:0:300}"
r="$(ingest GET '/api/v1/analysis/physical?period=60d' "$READ_TOKEN")"
[ "$(echo "$r" | field 'd["players"][0]["distance_km_p90"] > 5')" = True ] && ok "the squad's running per 90" || fail "season running: ${r:0:300}"

echo "== the game plans"
r="$(ingest GET /api/v1/plans/default "$READ_TOKEN")"
[ "$(echo "$r" | field 'd["doc"]["formation"]["out_of_possession"]')" = 4-4-2 ] && ok "the club's default plan" || fail "default plan: ${r:0:300}"
PLAN='{"title":"Smoke","formation":{"in_possession":"4-3-3"},"targets":[{"metric":"ppda","op":"<=","value":9,"key":true}],"by":"smoke"}'
r="$(ingest PUT "/api/v1/plans/$PAST" "$SITE_TOKEN" "$PLAN")"
[ "${r%% *}" = 401 ] && ok "a device's token can't write a plan" || fail "plan with a site token: $r"
r="$(ingest PUT "/api/v1/plans/$PAST" "$READ_TOKEN" '{"targets":[{"metric":"goals_by_magic","value":3}]}')"
[ "${r%% *}" = 400 ] && ok "an unknown metric is refused" || fail "bad plan: $r"
r="$(ingest PUT "/api/v1/plans/$PAST" "$READ_TOKEN" "$PLAN")"
[ "$(echo "$r" | field 'd["updated_by"]')" = smoke ] && ok "the staff save a match's plan" || fail "save plan: ${r:0:300}"
r="$(ingest DELETE "/api/v1/plans/$PAST" "$READ_TOKEN")"
r="$(ingest GET "/api/v1/plans/$PAST" "$READ_TOKEN")"
[ "$(echo "$r" | field 'd["inherited"]')" = True ] && ok "without its own plan, a match uses the club's default" || fail "inherited plan: ${r:0:300}"

echo "== the feed and the analyst's calls"
feed="$(ingest GET '/api/v1/feed?limit=60' "$READ_TOKEN")"
[ "$(echo "$feed" | field 'len(d["items"])')" = 60 ] && ok "the feed: $(echo "$feed" | field 'd["counts"]["pending"]') to call" || fail "feed: ${feed:0:300}"
[ "$(echo "$feed" | field 'any(i["kind"] == "event" and i.get("image") for i in d["items"])')" = True ] && ok "key plays are in it, with their keyframes" || fail "no key play in the feed: ${feed:0:300}"
dev="$(ingest GET "/api/v1/analysis/match?match_id=$PAST" "$READ_TOKEN" | field '[x["deviation_id"] for x in d["deviations"] if x["status"] != "pending_review"][0]')"
item="$SITE:$dev"
r="$(ingest POST "/api/v1/feed/$item/action" "$READ_TOKEN" '{"action": "reopen", "by": "smoke"}')"
[ "$(echo "$r" | field 'd["item"]["status"]')" = pending_review ] && ok "a call can be reopened" || fail "reopen: ${r:0:300}"
r="$(ingest POST "/api/v1/feed/$item/action" "$SITE_TOKEN" '{"action": "confirm"}')"
[ "${r%% *}" = 401 ] && ok "a device's token can't make a call" || fail "site-token call: $r"
r="$(ingest POST "/api/v1/feed/$item/action" "$READ_TOKEN" '{"action": "confirm", "by": "smoke", "note": "talk at the break"}')"
[ "$(echo "$r" | field 'd["item"]["status"]')" = confirmed ] && ok "a call through the feed, as p4n4-api makes it" || fail "call: ${r:0:300}"
for page in "analysis/:<title>Match Intelligence" "analysis/plan.html:Game plan" "analysis/report.html:Match report" "analysis/season.html:<title>Season" \
            "analysis/league.json:\"BHA\"" "analysis/marks/lion.svg:Premier League"; do
  r="$(ingest GET "/${page%%:*}")"
  [[ "$r" == "200 "*"${page#*:}"* ]] && ok "/${page%%:*} is served" || fail "/${page%%:*}: ${r:0:200}"
done
r="$(compose exec -T ingest python -c 'import urllib.request; r = urllib.request.urlopen("http://localhost:8080/analysis/crests/BHA.png"); print(r.status, r.headers["Content-Type"], r.read(4) == b"\x89PNG")')"
[ "$r" = "200 image/png True" ] && ok "the clubs' crests are served" || fail "crest: ${r:0:120}"
r="$(ingest GET "/analysis/../ingest.py")"
[ "${r%% *}" = 404 ] && ok "nothing outside the analysis room is" || fail "path traversal: ${r:0:120}"

echo "== access rules"
r="$(ingest GET /api/v1/feed)"
[ "${r%% *}" = 401 ] && ok "the feed needs a token" || fail "unauthenticated feed: $r"
r="$(ingest GET /api/v1/agent/match other-club-token)"
[ "${r%% *}" = 400 ] && ok "another club's token sees none of this one's matches" || fail "cross-site read: ${r:0:200}"
HB='{"schema":"p4n4.team.heartbeat/0.1","site_id":"'$SITE'","timestamp":"'$(date -u +%Y-%m-%dT%H:%M:%SZ)'","device":{"hostname":"smoke"},"metrics":{"x":1},"pipeline":{},"status":"healthy"}'
r="$(ingest POST /api/v1/heartbeat wrong-token "$HB")"
[ "${r%% *}" = 401 ] && ok "writes need the device's token" || fail "bad token write: $r"
r="$(ingest POST /api/v1/heartbeat other-club-token "$HB")"
[ "${r%% *}" = 400 ] && ok "a token writes only its own club" || fail "cross-site write: $r"
r="$(ingest POST /api/v1/windows "$SITE_TOKEN" '{"schema":"p4n4.team.window/0.1","site_id":"'$SITE'","match_id":"NOPE"}')"
[ "${r%% *}" = 400 ] && ok "a window that breaks the contract is refused" || fail "bad window: ${r:0:200}"

echo "== InfluxDB"
influx_count() {
  compose exec -T influxdb influx query --org "$INFLUXDB_ORG" --raw "$1" \
    | grep -v '^#' | sed -n 2p | awk -F, '{print $NF}' | tr -d '\r'
}
for m in "team_window:minute:100" "player_run:distance_m:1000" "plan_check:minute:100" "team_play:count:50" "plan_deviation:count:2"; do
  IFS=: read -r name f min <<<"$m"
  count="$(influx_count "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -60d) |> filter(fn: (r) => r._measurement == \"$name\" and r._field == \"$f\") |> group() |> count()")"
  [ "${count:-0}" -ge "$min" ] && ok "$count $name points in InfluxDB" || fail "$name points: ${count:-0}"
done
health="$(influx_count "from(bucket: \"$INFLUXDB_BUCKET\") |> range(start: -1h) |> filter(fn: (r) => r._measurement == \"sensor_data\" and r.sensor == \"tracking_fps\") |> group() |> count()")"
[ "${health:-0}" -gt 0 ] && ok "device health stored as sensor_data" || fail "no heartbeat sensor_data"

echo "== MQTT"
if wait "$mqtt_pid" && [ -s "$WORK_DIR/mqtt.txt" ]; then
  ok "messages under brighton/$SITE/"
  grep -q '"play_id"' "$WORK_DIR/mqtt.txt" && ! grep -q '"track"' "$WORK_DIR/mqtt.txt" && ! grep -q '"pass_pairs"' "$WORK_DIR/mqtt.txt" \
    && ok "plays without their track, windows without their pass network" || fail "MQTT: $(head -c 300 "$WORK_DIR/mqtt.txt")"
else
  fail "no message under brighton/$SITE/"
fi

echo "== Grafana"
for _ in $(seq 1 30); do
  compose exec -T grafana wget -qO /dev/null http://localhost:3000/api/health 2>/dev/null && break
  sleep 2
done
python3 "$WORK_DIR/tests/check_dashboards.py" "$PROJECT" "$GRAFANA_USER" "$GRAFANA_PASSWORD" \
  "site=$SITE" "match=$PAST" "node=brighton-match-device-sim" || failed=1
for f in fav32.png grafana_icon.svg; do
  compose exec -T grafana wget -qO- "http://localhost:3000/public/img/$f" | cmp -s - "../theme/grafana/$f" \
    && ok "Grafana serves the league's $f" || fail "Grafana doesn't serve the league's $f"
done
# The menu and login logo is a content-hashed file named in Grafana's bundle
ICON="$(compose exec -T grafana sh -c 'grep -rhoE "static/img/grafana_icon\.[0-9a-f]+\.svg" /usr/share/grafana/public/build/*.js | head -1')"
compose exec -T grafana cat "/usr/share/grafana/public/build/$ICON" | cmp -s - ../theme/grafana/grafana_icon.svg \
  && ok "Grafana's menu and login show the league's lion" \
  || fail "Grafana's logo is $ICON now: update its mount in iot/docker-compose.yml"

echo "== ai layer: n8n (project $PROJECT-ai)"
compose_ai up -d --wait --quiet-pull n8n mailpit >/dev/null 2>&1 || { compose_ai logs --no-color --tail 30 n8n; exit 1; }
inbox() { compose_ai exec -T n8n wget -qO- "http://mailpit:8025/api/v1/$1"; }
mail() { python3 -c "import json,sys; ms=json.load(sys.stdin)['messages']; sys.exit(0 if ($1) else 1)"; }
SMOKE=EPL-SMOKE-BHA-ARS
smoke_match "$SMOKE" live
smoke_match "$SMOKE" half_time
smoke_match "$SMOKE" full_time
messages='{"messages": []}'
for _ in $(seq 1 40); do
  messages="$(inbox messages || echo '{"messages": []}')"
  echo "$messages" | mail 'len(ms) >= 3' && break
  sleep 2
done
echo "$messages" | mail 'any("Possession below plan" in m["Subject"] and m["To"][0]["Address"] == "staff@example.com" for m in ms)' \
  && ok "the key deviation reached the staff" || fail "deviation email: ${messages:0:600}"
echo "$messages" | mail 'not any("Width in possession" in m["Subject"] for m in ms)' && ok "the normal one stayed in the analysis room" || fail "normal deviation emailed"
echo "$messages" | mail 'any(m["Subject"].startswith("Half-time report: BHA 1-0 ARS") and m["To"][0]["Address"] == "staff@example.com" for m in ms)' \
  && ok "the half-time report reached the staff" || fail "half-time email: ${messages:0:600}"
echo "$messages" | mail 'any(m["Subject"].startswith("Match report: BHA 1-0 ARS") and m["To"][0]["Address"] == "coaches@example.com" and m["Attachments"] == 1 for m in ms)' \
  && ok "the match report reached the coaches, with its CSV" || fail "report email: ${messages:0:600}"
id="$(echo "$messages" | python3 -c 'import json,sys; print(next((m["ID"] for m in json.load(sys.stdin)["messages"] if "Possession" in m["Subject"]), ""))')"
html="$( [ -n "$id" ] && inbox "message/$id" | python3 -c 'import json,sys; print(json.load(sys.stdin)["HTML"])' || true)"
[[ "$html" == *"analysis/#$SITE/$SMOKE/$SMOKE.possession_pct.1"* ]] && ok "the email links to the deviation in the analysis room" || fail "link: ${html:0:300}"
r="$(compose_ai exec -T n8n wget -qO- "http://localhost:5678/webhook/brighton/report?match_id=$SMOKE&site_id=$SITE&part=half" || true)"
echo "$r" | python3 -c 'import json,sys; d=json.load(sys.stdin); d=d[0] if isinstance(d,list) else d; sys.exit(0 if d["part"] == "half" and d["deviations"] >= 2 else 1)' \
  && ok "a report on demand (GET /webhook/brighton/report)" || fail "report now: ${r:0:300}"
count="$(inbox messages | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["messages"]))')"
compose_ai exec -T n8n wget -qO /dev/null --header 'Content-Type: application/json' --header 'X-Notify-Token: wrong' \
  --post-data '{"event":"deviation","deviation":{"priority":"high"},"match":{}}' http://localhost:5678/webhook/brighton/deviation >/dev/null 2>&1 || true
sleep 3
after="$(inbox messages | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["messages"]))')"
[ "$after" = "$count" ] && ok "a post without the ingest service's token sends nothing" || fail "messages: $count, then $after"

echo "== extras layer: video (project $PROJECT-extras)"
# A test clip for each camera but the tactical one (the footage itself isn't copied), which stays
# the synthetic camera go2rtc draws without a loop; the TV camera's URL wins over its loop
cameras="$(python3 -c "import json; print(' '.join(c['id'] for c in json.load(open('$WORK_DIR/.p4n4.json'))['dashboard']['cameras']))")"
for clip in $cameras; do
  [ "$clip" = tactical ] && continue
  docker run --rm --user "$(id -u):$(id -g)" --entrypoint ffmpeg -v "$WORK_DIR/extras/clips:/out" alexxit/go2rtc:1.9.14 \
    -hide_banner -loglevel error -f lavfi -i testsrc2=size=320x240:rate=10 -t 2 -c:v libx264 -pix_fmt yuv420p "/out/$clip.mp4"
done
compose_extras up -d --wait --quiet-pull >/dev/null 2>&1 || { compose_extras logs --no-color --tail 30; exit 1; }
for src in $cameras; do
  magic=""
  for _ in $(seq 1 30); do
    magic="$(compose_extras exec -T video sh -c "wget -qO- 'http://localhost:1984/api/frame.jpeg?src=$src' | head -c 3 | od -An -tx1" 2>/dev/null | tr -d ' \n')" || true
    [ "$magic" = ffd8ff ] && break
    sleep 2
  done
  [ "$magic" = ffd8ff ] && ok "camera '$src' serves JPEG frames" || fail "camera '$src': no JPEG (got '$magic')"
done
# p4n4-clips: each loop as a file for the dashboard (an MP4's ftyp box), and nothing else in extras/clips
for clip in $cameras; do
  [ "$clip" = tactical ] && continue
  box="$(compose_extras exec -T clips sh -c "wget -qO- 'http://localhost:8080/$clip.mp4' | head -c 8 | tail -c 4" 2>/dev/null || true)"
  [ "$box" = ftyp ] && ok "p4n4-clips serves '$clip.mp4'" || fail "p4n4-clips: '$clip.mp4' isn't an MP4 (got '$box')"
done
for path in vision.json footage/index.json analysis/; do
  compose_extras exec -T clips wget -qO /dev/null "http://localhost:8080/$path" >/dev/null 2>&1 \
    && fail "p4n4-clips serves /$path" || ok "p4n4-clips doesn't serve /$path"
done
# The synthetic camera plays the live match's engine before its first frame (about 5 s): give it 12
bytes="$(compose_extras exec -T video sh -c "timeout 12 wget -qO- 'http://localhost:1984/api/stream.mjpeg?src=tactical' | wc -c" 2>/dev/null | tr -d ' \r' || true)"
[ "${bytes:-0}" -gt 100000 ] && ok "the synthetic tactical camera streams MJPEG ($bytes bytes in 12 s)" || fail "tactical MJPEG stream: ${bytes:-0} bytes"
video_log="$(compose_extras logs video 2>&1)"
grep -q "smoke=1" <<<"$video_log" && fail "the video log shows the camera URL" || ok "the video log leaves camera URLs out"
# go2rtc logs error=EOF when it stops a camera nobody watches any more (the stream test leaving): not an error
video_errors="$(grep -i "traceback\|error" <<<"$video_log" | grep -v "error=EOF" || true)"
[ -n "$video_errors" ] && fail "the synthetic camera logged an error: $(head -3 <<<"$video_errors")" \
  || ok "the synthetic camera draws the live match without errors"

if [ "$failed" = 0 ]; then echo "PASS"; else echo "FAILED"; fi
exit "$failed"

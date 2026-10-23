#!/usr/bin/env bash
# A fresh demo before a meeting: the season starts again, with nothing left
# from earlier runs (old matches, calls and plans made while rehearsing, the
# live matches of the last days).
#
#   iot/scripts/reset_demo.sh            # from the project directory, with the stack up
#
# 1. Stops the simulator and the ingest service.
# 2. Moves the ingest database and keyframes to iot/data/ingest/backup-<time>/
#    (gitignored; delete it yourself once you're sure).
# 3. Deletes the matches from InfluxDB (team_window, player_run, plan_check,
#    team_play, plan_deviation, deviation_review, team_fixture), so Grafana's season
#    starts again too. The device's health (team_node, sensor_data) stays.
# 4. Starts the ingest service again, and the simulator if iot/.env's
#    COMPOSE_PROFILES has "demo" (it posts the club's default plan, the past
#    matches with their plans and calls, and the live match so far).
# 5. Posts the recorded footage's matches and plays (extras/scripts/clips.py
#    post) when the footage's analyses are on this disk.
set -euo pipefail
cd "$(dirname "$0")/../.."

env_value() { grep -E "^$1=" iot/.env 2>/dev/null | tail -1 | cut -d= -f2- || true; }
ORG=$(env_value INFLUXDB_ORG); BUCKET=$(env_value INFLUXDB_BUCKET); TOKEN=$(env_value INFLUXDB_TOKEN)

echo "== stopping the simulator and the ingest service"
(cd iot && docker compose stop simulator ingest)

backup="iot/data/ingest/backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$backup"
for f in ingest.db ingest.db-wal ingest.db-shm evidence; do
    if [ -e "iot/data/ingest/$f" ]; then mv "iot/data/ingest/$f" "$backup/"; fi
done
echo "== the old database and keyframes are in $backup"

echo "== deleting the matches from InfluxDB"
for m in team_window player_run plan_check team_play plan_deviation deviation_review team_fixture; do
    docker exec p4n4-influxdb influx delete --org "${ORG:-ming}" --bucket "${BUCKET:-raw_telemetry}" \
        --token "${TOKEN:-p4n4-stack-token}" --start 1970-01-01T00:00:00Z --stop "$(date -u -d '+1 day' +%Y-%m-%dT%H:%M:%SZ)" \
        --predicate "_measurement=\"$m\""
done

echo "== starting the ingest service"
(cd iot && docker compose up -d --wait ingest)
if [[ ",$(env_value COMPOSE_PROFILES)," == *,demo,* ]]; then
    echo "== starting the simulator (it plays and measures the past matches first: about a minute)"
    (cd iot && docker compose up -d simulator)
fi

if ls extras/clips/analysis/*/alerts.json >/dev/null 2>&1; then
    echo "== posting the recorded footage's matches (what the vision models found in it)"
    INGEST_URL=http://localhost:8090 python3 extras/scripts/clips.py post
fi
echo "== done: http://localhost:8090/analysis/ (the simulator's past matches arrive over the next minute)"

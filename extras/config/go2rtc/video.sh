#!/bin/sh
# ==============================================================================
# Writes go2rtc's config from the environment, then runs go2rtc. Two streams,
# which .p4n4.json lists as the dashboard's cameras and the analysis room
# opens by the camera id each play names:
#
#   tactical     CAMERA_TACTICAL_URL: high on the main stand at halfway, the
#                whole pitch in view (the one the tracker measures)
#   tv           CAMERA_TV_URL: the broadcast feed (e.g. an SRT or RTSP output
#                of the broadcaster's truck, or a capture card's stream)
#
# A URL is rtsp://user:pass@ip:554/stream, srt://…, an http:// MJPEG stream, …;
# it is transcoded to MJPEG for the dashboard when it isn't MJPEG already.
# Without its URL, a stream loops extras/clips/<name>.mp4 at real speed (e.g.
# licensed footage for a demo); without that, pitch.py draws a synthetic
# camera drawing the demo's live match, marked SYNTHETIC. Any other loop there
# (extras/clips/<id>.mp4, ids of lowercase letters, digits and dashes) is a
# stream of its own: recorded footage extras/scripts/clips.py cuts (the team's
# own kickabout, filmed for the vision models).
#
# Every stream is served at http://<host>:1984/api/stream.mjpeg?src=<name>,
# and as a snapshot at /api/frame.jpeg?src=<name>. The config lives in /tmp,
# so the API can't rewrite it; go2rtc refuses exec: sources from its API.
# ==============================================================================
set -eu

CONFIG=/tmp/go2rtc.yaml
CLIPS=/p4n4/clips

# A single-quoted YAML scalar: quotes are doubled, nothing else is special
quote() { printf "'%s'" "$(printf '%s' "$1" | sed "s/'/''/g")"; }

# A clip from $CLIPS, looped forever at its own pace, as MJPEG
loop() { echo "exec:ffmpeg -hide_banner -loglevel error -re -stream_loop -1 -i $CLIPS/$1.mp4 -an -c:v mjpeg -q:v 5 -f mjpeg -"; }

synthetic=""
{
  echo "streams:"
  for name in tactical tv; do
    var="CAMERA_$(echo "$name" | tr 'a-z-' 'A-Z_')_URL"
    url="$(eval "printf '%s' \"\${$var:-}\"")"
    echo "  $name:"
    if [ -n "$url" ]; then
      echo "    - $(quote "$url")"
      # Used only when the camera's own stream isn't MJPEG (e.g. H.264 over RTSP)
      echo "    - 'ffmpeg:$name#video=mjpeg'"
    elif [ -f "$CLIPS/$name.mp4" ]; then
      echo "    - $(quote "$(loop "$name")")"
    else
      echo "    - 'exec:python3 /p4n4/pitch.py $name'"
      synthetic="$synthetic $name"
    fi
  done
  for clip in "$CLIPS"/*.mp4; do
    [ -f "$clip" ] || continue
    name=$(basename "$clip" .mp4)
    case "$name" in tactical | tv | *[!a-z0-9-]*) continue ;; esac
    echo "  $name:"
    echo "    - $(quote "$(loop "$name")")"
  done
  echo "api:"
  echo "  listen: ':1984'"
  echo "rtsp:"
  echo "  listen: ':8554'"
} > "$CONFIG"

# Which sources, not their URLs: those can carry a camera's password
for name in tactical tv; do
  var="CAMERA_$(echo "$name" | tr 'a-z-' 'A-Z_')_URL"
  if [ -n "$(eval "printf '%s' \"\${$var:-}\"")" ]; then from="$var"
  elif [ -f "$CLIPS/$name.mp4" ]; then from="clips/$name.mp4"
  else from="synthetic"; fi
  echo "video: $name from $from"
done

# The synthetic cameras draw the pitch from a background rendered once
if [ -n "$synthetic" ]; then
  python3 /p4n4/pitch.py --prepare
fi
exec go2rtc -config "$CONFIG"

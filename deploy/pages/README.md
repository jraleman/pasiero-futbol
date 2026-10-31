# The static site (GitHub Pages)

The demo without a stack: everything the staff see of the club's latest matches, recorded from the match device
and served as files. Nothing runs behind it, and every match in it is synthetic.

| Where | What |
|---|---|
| `/` | The dashboard: p4n4-dashboard's in-browser demo build in the theme in use. Sign in as `admin`, `power` (the analyst) or `normie` (the head coach), password `pasiero` (`PAGES_PASSWORD`). Home, the feed (deviations and key plays, one recorded match a day, the newest yesterday), the cameras (the five loops and their goals), Grafana (the snapshot below), the assistant (its recorded answers), services and device (made up). |
| `analysis/` | The analysis room, plan editor, season page and printable reports, unchanged but for `static.js`, which answers the ingest API from the recording. Sign in with any name; the token isn't checked. **BHA v HUL, the live match the demo was presented on, plays again as it was played**: a bar at the bottom shows the minute, pauses, changes the speed (×3 as on the stack, ×10, ×30), jumps to a moment (the goals, each deviation, the shape changes, half and full time) and starts again; the key deviations and the reports pop up as they reached the staff's phones and inboxes. |
| `grafana/` | Grafana's *Match analysis* board, a snapshot per recorded match, dark and light. |
| `reports/` | The emails n8n sent the staff about those matches: the deviations, #7's running, the half-time and match reports. |

What's recorded, and what isn't:

- **Replayed**: BHA v HUL minute by minute (`replay.py`): the simulator plays it again from its seed into a throwaway
  ingest service on the match's own clock, and what the analysis room asks is kept after every minute. It is the
  recording to the second (plays, numbers, deviations), with the club's default plan, as during the match (the
  simulator saves a later live match's own plan only after full time). While it plays, the matches after it are
  hidden and its story waits for full time (the stories were written then).
- **Recorded**: the club's 8 newest finished matches and the 4 SoccerTrack goals (every play, its 2D replay and
  keyframe, the plans, the deviations and the calls on them), the season pages, the feed, the emails, Grafana.
- **The narrator** told each of the 3 newest matches' story for every audience in English, and for a fan in Spanish
  following #7, and for the coaching staff in Spanish; the older matches' for a fan and for broadcast. Other choices
  get the closest recorded story, and say which it is.
- **The assistant** answers the brand's suggested questions with what the device's own agent team answered
  (`gemma4:e2b` on the Jetson), asked after the newest match's full time. Other questions get the list of those.
- **Stays in the browser**: calls on deviations, notes and saved plans in the analysis room (on every page of it); the
  dashboard's reviews and settings until the page reloads.
- **Not here**: live video (the analysis room's *Live camera* says it doesn't answer; the keyframe and the 2D replay
  work), the story and the assistant for anything not recorded.

## Rebuilding

```bash
# 1. On the device, after a match's full time (its report is in Mailpit): pause the live matches
ssh <user>@<device> docker stop p4n4-simulator
# 2. Record (about half an hour on the Jetson: the stories and answers are the model's), then read the stories
#    (recording/stories/) and answers (recording/answers.json); delete one to ask again, and run again
CHROMIUM=/usr/bin/chromium NODE_PATH=$(npm root -g) python3 deploy/pages/record.py
ssh <user>@<device> docker start p4n4-simulator
# 3. The replay (needs nothing running; PAGES_REPLAY / --match for another live match)
python3 deploy/pages/replay.py
# 4. Build (Flutter and p4n4's sources beside this repo: P4N4_SRC), look, publish
python3 deploy/pages/build.py
# To look first, under the same path as on Pages: http://localhost:8099/pasiero-futbol/
mkdir -p /tmp/pages && ln -sfn "$PWD/deploy/pages/site" /tmp/pages/pasiero-futbol && python3 -m http.server -d /tmp/pages 8099
python3 deploy/pages/build.py --no-flutter --publish
```

`--publish` pushes the site as the only commit of `gh-pages` on `origin`; in the repository's Settings → Pages,
deploy from the `gh-pages` branch, `/`. A private repository needs a plan with Pages for private repositories, and
the site is public either way.

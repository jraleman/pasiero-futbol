# Demo video: Pasiero Futbol (under 2 minutes)

The hackathon asks for a video **under two minutes**, showing the project working on the platform it was built for, with no third-party trademarks, copyrighted music or other material without permission. This is its script, and how it was recorded.

## The platform

The club's match device: an **NVIDIA Jetson Orin Nano** (8 GB), the whole stack on it ([deploy/jetson/up.sh](deploy/jetson/up.sh)): the match engine, the ingest service and the analysis room, InfluxDB, Grafana, MQTT, n8n and Mailpit, the cameras, and Ollama on the Jetson's GPU with the **Microsoft Agent Framework** team running on it (`AGENT_BACKEND=local`), offline. The dashboard is p4n4-dashboard, pointed at the Jetson. The cameras are four goals from SoccerTrack v2 (CC BY 4.0: A. Scott, I. Uchida, K. Kuroda, Y. Kim, K. Fujii, arXiv:2508.01802); the organisers allow footage in the demo, and the credit is on screen whenever it is.

## Before recording

1. **The Jetson.** `P4N4_SRC=../p4n4 deploy/jetson/up.sh <user>@<host>` with nothing else running on the board, then the SSH tunnel and the dashboard as its header says, the dashboard's host set to the Jetson's address. The header then shows the Jetson's address.
2. **Time it to the live match.** `docker exec p4n4-simulator python /scripts/simulate.py --schedule` on the Jetson prints when the live match reaches each moment (UTC). The deviations open at 35'–40', the half-time report lands about a minute after the whistle, #7's legs go at 65'–70'. A live match lasts 37 minutes, and the next kicks off 44 minutes after the last.
3. **The screen.** 1920×1080, dark mode, in the Pasiero Futbol theme (`python3 iot/scripts/theme.py use pasiero-futbol`, then restart Grafana and the ai layer and rebuild the dashboard's image). No music, or music you have the rights to. The clubs' crests are on screen (the team's call: the organisers' clearance covers them).

## The script

| Time | On screen | Voice over |
|---|---|---|
| 0:00–0:07 | A phone clip of the Jetson on the desk, its cable and fan (filmed, not generated). Cut to the dashboard: the header reads the Jetson's address | "This is a club's match device: an NVIDIA Jetson. Everything you'll see runs on it, at the ground, with no cloud." |
| 0:07–0:24 | The dashboard's Cameras: one camera, the players boxed in their teams' colours; a goal toast comes up, tap it: the camera holds on the goal. Caption: *Footage: SoccerTrack v2, CC BY 4.0* | "It watches the cameras: every player boxed and tracked, the two teams told apart by their kits with no training, and the moments that matter flagged as they happen." |
| 0:24–0:44 | The analysis room's sign-in: the mascot's *¡Hola! I'm Pasiero* over the tactics doodles, the name and token typed. Then the live match at about 40': the scorecard turns, *Team length* ✕, *Defensive line height* ✕. Open the deviation: what was measured, against the plan. Press **F**, type a note for the bench | "And it measures every match every five minutes against the staff's game plan. It doesn't just report that something happened, it explains it: the line dropped against the height the plan asked for. The analyst flags it for the bench." |
| 0:44–0:54 | Running: the bars, a top-speed milestone, #7 Tomas Calloway's drop at 70' | "Every player's running, with milestones as they happen: a reading for the staff, never a verdict." |
| 0:54–1:18 | The dashboard's Assistant: *"How are we doing, and what should we tell the players?"* The steps: *the analyst · match summary* and *the coach's brief · briefing* together; a caption, *1 minute later*; the answer | "Behind the assistant is a team of AI agents on Microsoft Agent Framework: an orchestrator calls an analyst for the numbers and a coach for the options, at the same time, every number from the match device's own tools. Here the whole team runs on the Jetson's GPU." |
| 1:18–1:36 | The analysis room's Match story: *A fan*, *Español*, *Follow #7*; then *Broadcast captions* | "A narrator agent turns the same numbers into the story each audience needs: for a fan, in Spanish, following one player, or as captions for a broadcast." |
| 1:36–1:46 | Mailpit: the half-time report, the story of the half, the plan's targets and the deviations with the analyst's note | "At half time, the report is on the coaches' phones before they reach the dressing room." |
| 1:46–1:55 | The repository: `deploy/azure` (Terraform) and `ai/vision/shirts` (the Azure Machine Learning job) | "The same stack is built to deploy on Azure from Terraform, with Azure OpenAI for the agents, and our shirt-number model trains on Azure Machine Learning." |
| 1:55–2:00 | The sign-in page: the logo, *Pasiero Futbol*; the repository's URL (github.com/jraleman/pasiero-futbol) | "Pasiero Futbol: every match, for every fan." |

**Keep it under 2:00:** judges needn't watch beyond two minutes. Cut the repository shot first if it runs long. The Assistant's answer takes about 80 seconds on the Jetson: the cut says so (*1 minute later*), it isn't sped up.

**Check every answer before it goes in.** The Jetson's model (gemma4:e2b, 4.6B) is less exact than Azure OpenAI: in testing, a broadcast caption once named a minute the match hadn't reached. Re-ask, or pick another take.

## Questions judges may ask

- **"Is this real Premier League data?"** No: every match is synthetic, played by a match engine 5 times a second and marked *Demo data* on every screen. The clubs and their crests are the league's; the players' names are made up. The camera footage is real university football (SoccerTrack v2, CC BY 4.0), with the teams named by their kits.
- **"What do the agents actually do?"** The orchestrator reads the question and the view asking, and calls the specialists it needs as tools (Agent Framework's agents as tools); the analyst, coach and narrator call the ingest service's tools. A function middleware streams each call to the chat. On the Jetson the team runs on Ollama's model through its OpenAI-compatible API; with Azure OpenAI configured, the same team runs there, and Ollama is the fallback.
- **"Can a model invent a number?"** The tools return the match device's numbers and sentences; the prompts tell every agent to repeat them exactly, and the recaps are written from facts gathered before the narrator starts. The tests check that the answer carries the ingest service's own numbers. A small model on the device is the weak point: it can still slip, which is why the video's answers were checked.
- **"Why train your own number model?"** The vision-language model takes seconds a crop; ours takes milliseconds. On 212 hand-checked crops of real players, though, the vision-language model read 72% of the legible numbers and ours 7% (it never invents one): ours was trained on synthetic players, and the panorama's small, blurred shirts are too far from them. The vision-language model stays the reader ([ai/vision/shirts](ai/vision/shirts/README.md)).
- **"How would it reach fans?"** The same recap endpoint takes an audience, a language and a player to follow; MQTT carries every window, play and deviation, timed by match minute, for anything that draws over a broadcast.

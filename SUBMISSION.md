# Hackathon submission: Premier League Match Intelligence

How this project meets the hackathon's criteria ([CHALLENGE.md](CHALLENGE.md)), criterion by criterion, with where to see each one. The full description is in [README.md](README.md); the video's script is in [DEMO.md](DEMO.md).

**Status:** ✓ covered · ◐ partly · ✗ not covered.

## The pitch

> **Match Intelligence turns synthetic football events into explainable match intelligence, checked against the game plan and told for every audience.** A match device measures every match every five minutes: the press, the shape, set pieces, pass quality, ball and shot speed, and every player's running. It checks each number against the staff's game plan, and when the plan slips it explains why, from when, and what the plan asked for. A team of AI agents built with **Microsoft Agent Framework** on **Azure OpenAI** works from those numbers alone. An orchestrator calls an analyst, a coach and a narrator. They answer the staff's questions, give the bench options rather than orders, and tell each match's story for a fan, a coach, an analyst or a broadcast's captions, in 14 languages, following one player if asked. It renders in a live analysis room, Grafana, the dashboard's Video tab and n8n's reports to the staff's phones. It falls back to a local model when there's no network.
>
> **The problem:** clubs, broadcasters and fans get the scoreline and a wall of stats, not the story. Match Intelligence surfaces the moments that matter, explains why they mattered, and makes every match feel made for every fan.
>
> **Microsoft and Azure:** Microsoft Agent Framework (multi-agent orchestration, agents as tools, function middleware); Azure OpenAI (the agents' model, keys off, by the VM's managed identity through Microsoft Entra ID); Azure Virtual Machines (the whole stack, held to a club device's limits); Azure Key Vault (every secret); Azure Machine Learning (our own shirt-number model, trained on synthetic players, exported to ONNX); all from Terraform, deployed by GitHub Actions with OIDC.

## How to run it

| | |
|---|---|
| **The full stack** | README, Quick start: `for layer in iot ai extras; do cp $layer/.env.example $layer/.env; done`, set `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_DEPLOYMENT` (and a key, or Entra ID) in `ai/.env`, then `p4n4 up` (or `docker compose up -d` in `iot`, `ai`, `extras`, in that order). Analysis room <http://localhost:8090/analysis/> (token `demo-read-token`), Grafana <http://localhost:3000>, emails <http://localhost:8025>. A live match is always playing, sped up ×3; `python3 iot/scripts/simulate.py --schedule` prints when it reaches each moment. |
| **Hosted on Azure** | `cd deploy/azure && terraform apply` ([deploy/azure/README.md](deploy/azure/README.md)): the VM deploys itself in about 20 minutes. The dashboard at `https://<name>.uksouth.cloudapp.azure.com/` (user `judge`), the analysis room at `:8443/analysis/`, the inbox at `:8443/mail/`; logins from `terraform output judge_login`. |
| **Tests** | `python3 tests/test_analytics.py`, `tests/test_ingest.py`, `tests/test_local_tools.py`, `tests/test_team.py` (no Docker; the last needs `pip install -r ai/agent/requirements.txt`), `tests/test_shirts.py` (the shirt-number model); `terraform test` in `deploy/azure` (mock providers); `./tests/smoke.sh` (the whole stack, throwaway). GitHub Actions runs all but the smoke test on every push. |
| **Licence** | MIT ([LICENSE](LICENSE)). The Premier League's and the clubs' marks, the fonts and the vision pipeline's AGPL-3.0 parts are not covered ([NOTICE.md](NOTICE.md)). |

## (i) One pipeline with five stages

| Stage | Status | What we have | Where |
|---|---|---|---|
| **(a) Ingest** synthetic events as they happen | ✓ | A match engine plays every match 5 times a second: every player and the ball, plus the on-ball events (passes with receiver and ball speed, carries, shots with their speed, tackles, interceptions, clearances, fouls, restarts with their routine), and the nearest defender closing the ball down. The device turns these into 5-minute windows and technical plays and posts them live (`"final": false` while a window is being played) through a versioned contract: fixtures (match metadata), windows, plays, heartbeat. | `iot/scripts/engine.py`, `simulate.py`, `iot/ingest/contract.py` |
| **(b) Interpret** into statistics, patterns and context | ✓ | Possession, pass completion, pass quality (length, a 0–1 difficulty, hard passes completed), ball and shot speed, PPDA, field tilt, final-third entries by channel, shots and xG, turnovers and regains, set pieces (routine, delivery, first contact, xG), the shape in and out of possession (formation by optimal assignment, line height, width, length, the pass network) and every player's running. Plays: goals and how they came, shots, set pieces, high regains, counter-attacks, build-ups, presses played through, fouls won, formation changes. | `iot/scripts/analytics.py` |
| **(c) Explain** why a moment matters | ✓ | Every 5 minutes is checked against the staff's game plan. A **deviation** says what was measured, against what target, over which stretch, what to check, and the plan's own instruction for it, then closes "back on plan at 60'". Every play has a sentence built only from its measured fields. The **narrator agent** explains why moments mattered: control against chaos, the press, the plan holding or slipping, the goals and how they came, a player's legs going. | `iot/ingest/plan.py`, `store.py`, `ai/agent/team.py` |
| **(d) Render** on screen alongside the match | ✓ | The analysis room (scorecard, shape, timeline, set pieces, running, the Match story, a 2D replay of every play), Grafana, the dashboard's Video tab with the vision overlay, **broadcast captions** (3 to 5 lower-third lines under 80 characters, minute first). It's machine-readable and timed by match minute: MQTT `brighton/<site>/window`, `play/<type>`, `deviation`; the feed API; the recap API. | `iot/ingest/analysis/`, `iot/ingest/sinks.py` |
| **(e) Personalize** for an analyst and a casual fan | ✓ | The same numbers, as different experiences. The **analyst** gets the calls, numbers, minutes and play ids. The **coaching staff** get options in football language. A **fan** gets the story in plain words, with each number explained the first time. **Broadcast** gets captions. Each comes in **14 languages**, or any language by name. **Player focus**: follow one of our players through the story (his part in the moments, his running). Views have their own starter questions and follow-ups. A favourite club works by data: the device is per club (`league.json` `club`), and every view takes our side. | The analysis room's Match story; `team.py` `recap`; `theme/brand.json` |

## (ii) Real-time match intelligence

| Feature | Status | What we have | Where |
|---|---|---|---|
| **Player identification**: name tagging | ✓ | Every shirt number of every club has a made-up name, seeded per club. The analysis room, sentences, deviations, reports, emails, the agents and the recaps all say "#7 Tomas Calloway". No real player is named. | `iot/scripts/squads.py` → `squads.json`, `Store.player_label` |
| …with speed and distance indicators at thresholds | ✓ | **Milestones** tag a player the first time he passes 30 km/h, and at 5 and 10 km, on the 5 minutes he did it in. Every window carries each player's distance, high-speed running (> 19.8 km/h), sprints (> 25.2 km/h) and top speed. | `store.py` (`SPEED_TAG_KMH`, `DISTANCE_TAGS_KM`), the Running panel |
| **Pass quality**: distance, accuracy, difficulty | ✓ | Pass completion, average pass length, a difficulty rating per pass (length, forward progress, pressure on the receiver, into the final third), and hard passes completed. | `analytics.py` (`pass_difficulty`), Team data |
| **Speed metrics**: ball speed and shot speed | ✓ | Average pass speed and the fastest ball per window, and the fastest shot. Every goal and shot carries its speed. The device's ball-tracker reading is used when it sends one; otherwise the speed is measured from the ball's track. | `analytics.py` (`ball_speed`) |
| **Auto-eventing** from a video feed | ◐ | The vision pipeline boxes every player and the ball on recorded footage, tells the teams apart by their kits (no training needed for a change of kit), reads shirt numbers (a vision-language model, or **our own model trained on Azure Machine Learning** on synthetic players: every kit, 16 fonts, and the cases where a person couldn't read the number, so it learns not to guess), and finds contacts and falls as plays. These go in through the same contract as `source: video`. It runs on four goals from SoccerTrack v2 (CC BY 4.0, pending the organisers' answer on real footage) until our own kickabout is filmed. On 212 hand-checked crops of real players from that dataset, the vision-language model read 72% of the legible numbers and ours 7%, inventing none, so the vision-language model stays the reader (`matchvision.numbers_eval`). Pass and speed metrics from video need a fixed, calibrated camera. | `ai/vision/matchvision/`, `ai/vision/shirts/`, `extras/scripts/clips.py` |

## (iii) Narrative generation

| | Status | What we have | Where |
|---|---|---|---|
| Key narratives, player-performance context, milestones, recaps | ✓ | The **narrator agent's recaps**: the match so far, the first half or the whole match, per audience and language, optionally following one player. They're written from facts gathered first by the same tools, so the narrator can't invent a moment. The half-time and match **reports** reach the staff by email (n8n) with the narrator's story first, then the plan target by target, the deviations and the analyst's notes, set pieces, the running, and the key plays. The printable report is a separate A4 page. A sentence for every play and every deviation. | `ai/agent/team.py` (`recap`), `ai/config/n8n/`, `report.html` |

## (iv) Explainability

| | Status | What we have | Where |
|---|---|---|---|
| Control vs chaos | ◐ | **Control:** possession, field tilt, pass completion, hard passes completed, final-third entries. **Chaos:** turnovers, regains, high regains, counter-press, presses played through, counter-attacks. These are shown together per 5 minutes and per part of the match, and the narrator explains the balance in words. There's no single "control vs chaos" index. | Team data, Grafana, the narrator |
| Tactical pressure changes | ✓ | PPDA and the counter-press every 5 minutes against the plan. A deviation opens when the press fades. Formation changes are plays ("they change to a back three in possession"). | The scorecard, Deviations, the timeline |
| Game rhythm | ◐ | Every metric every 5 minutes; xG per 5 minutes on the timeline; each player's metres a minute as bars, and the running drop as the legs go. There's no explicit tempo model. | The timeline, Running |

## (v) Data innovation

| | Status | What we have | Where |
|---|---|---|---|
| Synthetic football-realistic datasets | ✓ | The engine plays a whole season from seeds: tracking at 5 Hz for 22 players and the ball, on-ball events with ball-tracker readings, pressure, set-piece routines, substitutions and tiring legs, plus match metadata. It measures like football: about 600 passes a team at 80%, 10–25 shots, xG 1–2, PPDA 5–14, 8.5–12.5 km per outfield player. Every document is marked `"synthetic": true`. A scripted story (a goal after a high regain, the press fading, a set-piece goal, a player's legs going) makes the live match tell the same story every time, for demos and tests. Squads of made-up names. | `iot/scripts/engine.py`, `simulate.py`, `squads.py` |

## (vi) Multi-language storytelling

| | Status | What we have | Where |
|---|---|---|---|
| Natural-language outputs in several languages | ✓ | The narrator writes recaps and broadcast captions in 14 languages from the analysis room (English, Spanish, Portuguese, French, German, Italian, Dutch, Arabic (right to left), Japanese, Korean, Chinese, Hindi, Swahili, Turkish), or any language named through the API. The reports' story is in `RECAP_LANGUAGE`. The assistant answers in the language of the question. Fixed sentences (play summaries, deviation titles) stay in English. | `team.py`, the Match story, `ai/.env` |

## Microsoft and Azure, in the code

| Technology | What it does here | Where |
|---|---|---|
| **Microsoft Agent Framework** 1.23 | The team: four `Agent`s; the specialists given to the orchestrator as tools (`Agent.as_tool`); the ingest service's ten tools as `FunctionTool`s with their JSON schemas; a `FunctionMiddleware` that streams every tool call to the chat; streaming runs | `ai/agent/team.py`, `tests/test_team.py` |
| **Azure OpenAI** | The agents' model (`OpenAIChatCompletionClient` on the Azure deployment, `gpt-4.1-mini`), by Microsoft Entra ID (`DefaultAzureCredential`: the VM's managed identity); the resource has keys off | `team.py` `azure_client`, `deploy/azure/openai.tf` |
| **Azure Virtual Machines** | The whole stack on one VM, under p4n4-emu's `nuc` profile (a club device's 4 cores and 14 GB), behind HTTPS (Caddy, Let's Encrypt) on its `cloudapp.azure.com` name; only 80, 443 and 8443 open | `deploy/azure/main.tf`, `vm/` |
| **Managed identities, Microsoft Entra ID** | The VM reads Key Vault and calls Azure OpenAI with no key on disk; GitHub Actions signs in with OIDC (a federated credential) to redeploy, allowed only to run commands on that VM | `deploy/azure/keyvault.tf`, `github.tf`, `.github/workflows/deploy.yml` |
| **Azure Key Vault** | Every secret (ten, generated by Terraform), read by the VM on each deploy | `deploy/azure/keyvault.tf`, `vm/deploy.sh` |
| **Microsoft Cost Management** | A monthly budget on the deployment's resource group, emailing at 50%, 80% and 100% spent and on a forecast over budget | `deploy/azure/budget.tf` |
| **Azure Machine Learning** | Our shirt-number model: a command job on a CPU cluster that scales to zero, synthetic players drawn as it trains, the run's metrics in MLflow, the model registered and exported to ONNX for ONNX Runtime | `ai/vision/shirts/` (`job.yml`, `train.py`, `aml.sh`), `deploy/azure/ml.tf` |

## Notes

- **Data.** Synthetic only. The real tracked match and the real footage the project once used were removed.
- **Marks.** The Premier League's logo and the clubs' crests are Wikipedia's copies of their logos. The rules forbid third-party trademarks in the video without permission, so confirm with the organisers that the category allows them before uploading.
- **Players.** The contract carries shirt numbers only; the names are made up. No player ratings.

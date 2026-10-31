# Pasiero Futbol

![Pasiero Futbol](pasiero-futbol-banner.png)

**Match intelligence for the Premier League.**

Pasiero Futbol checks a club's game plan *while the match is played*, and turns the numbers into the story each audience needs: the analyst, the coach, the broadcast and the fans.

**[Try the live demo →](https://www.jraleman.com/pasiero-futbol/analysis/)** · **[See the hackathon submission →](https://innovationstudio.microsoft.com/hackathons/insidethegamedeveloperhackathon/project/126890)**

The demo replays the match from the video as it was played (sign in with any name). The dashboard is at [the site's root](https://www.jraleman.com/pasiero-futbol/) (`admin`, `power` or `normie`, password `pasiero`).

Built for the hackathon in [CHALLENGE.md](CHALLENGE.md) · how it meets each criterion: [SUBMISSION.md](SUBMISSION.md) · the demo video's script: [DEMO.md](DEMO.md)

> Every match here is synthetic, and says *Demo data* on every screen. The demo's club is Brighton & Hove Albion; its players have made-up names.

## What it does

- **Measures** every match, 5 minutes at a time: possession, passing, the press, shape, set pieces, shots and xG, and every player's running.
- **Checks the game plan.** When a target slips for 15 minutes, it says what was measured, against what, and the plan's own instruction for it.
- **Explains.** A team of AI agents answers the staff's questions and tells the match's story, for a fan, a coach, an analyst or broadcast captions, in any language.
- **Delivers.** The analysis room on the booth's screen, Grafana, and the half-time report on the coaches' phones before they reach the dressing room.

It measures and compares; every call stays with the staff. No player ratings.

## Built with

| | |
|---|---|
| **Microsoft Agent Framework** | The team of agents: an orchestrator, an analyst, a coach and a narrator |
| **Azure OpenAI** | The team's model, by managed identity or a key |
| **Azure VM, Key Vault, managed identities** | The whole stack, hosted privately, from Terraform ([deploy/azure](deploy/azure/README.md)) |
| **Azure Machine Learning** | Our own shirt-number model ([ai/vision/shirts](ai/vision/shirts/README.md)) |
| **Offline fallback** | No Azure? A local model on Ollama answers instead |
| Also | InfluxDB, Grafana, Mosquitto, n8n, go2rtc, on p4n4's template |

## Quick start

```bash
for layer in iot ai extras; do cp $layer/.env.example $layer/.env; done   # then change the passwords and tokens
p4n4 up                                                                   # iot, then ai, then extras
```

That's it: a match engine starts playing a season, and the dashboards fill in over a minute or two.

- **Want the Azure agents?** Set `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT` and `AZURE_OPENAI_API_KEY` (or leave the key empty for Entra ID) in `ai/.env`.
- **No p4n4 CLI?** Run `docker compose up -d` in `iot`, `ai` and `extras`, in that order.
- **On Azure:** `cd deploy/azure && terraform apply` ([guide](deploy/azure/README.md)).
- **On an NVIDIA Jetson:** `deploy/jetson/up.sh <user>@<host>` ([up.sh](deploy/jetson/up.sh)).
- **Fresh start before a demo:** `iot/scripts/reset_demo.sh`.

Stop any other p4n4 project on the host first: they share container names and ports.

### Then open

| Open | What | Login |
|---|---|---|
| <http://localhost:8090/analysis/> | The **analysis room** | Your name + `demo-read-token` |
| <http://localhost:3000> | **Grafana** | `admin` / `adminpassword` |
| <http://localhost:8025> | **Mailpit**: every report and alert email | none |
| <http://localhost:5678> | **n8n**: the alert and report workflows | `admin@example.com` / `adminpassword` |

## The live match

A match is always playing at the Amex, sped up 3×, and it tells the same story every time:

| Minute | What happens |
|---|---|
| 19' | #10 wins it high, #9 scores · **1-0** |
| 25'–45' | The line drops, the press fades: **off plan** |
| 41' | Their counter plays through the press · **1-1** |
| Half time | The half-time report reaches the staff |
| 46'–60' | Back on plan after the talk |
| 63' | The near-post corner routine: #5 heads it in · **2-1** |
| 65'–70' | #7's legs go: "running 29% less than his first 30'", and the bench's phones get it |
| 72' | #7 off, #19 on |
| Full time | The match report reaches the coaches |

`python3 iot/scripts/simulate.py --schedule` tells you when the next match reaches each moment.

## How it fits together

```
 match device (or the simulator) ──► ingest ──► analysis room · Grafana · MQTT
                                        │
                                        ├──► n8n ──► email · Slack · Telegram
                                        │
                                        └──► agents ──► Azure OpenAI (or Ollama offline)
```

| Folder | What's in it |
|---|---|
| `iot/` | The ingest service, the analysis room, the simulator, InfluxDB, Grafana |
| `ai/` | The agents, Ollama, n8n, the vision pipeline |
| `extras/` | Recorded footage and the cameras |
| `themes/` | The looks: `pasiero-futbol` (in use) and `pl-match-intelligence` |
| `deploy/` | Azure, Jetson and the static demo site |

## Testing

```bash
python3 tests/test_analytics.py && python3 tests/test_ingest.py   # and the rest of tests/
./tests/smoke.sh                                                   # the whole stack, end to end
```

GitHub Actions runs them on every push ([ci.yml](.github/workflows/ci.yml)).

## Learn more

Everything else (the game plan's metrics, the data contract, the agents, video, themes, security) is in **[docs/REFERENCE.md](docs/REFERENCE.md)**.

## License

The code is MIT ([LICENSE](LICENSE)). The club crests, league marks, fonts and some vision parts are not: see [NOTICE.md](NOTICE.md).

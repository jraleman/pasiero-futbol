# Demo video: Premier League Match Intelligence (under 2 minutes)

The hackathon asks for a video **under two minutes**, showing the project working on the platform it was built for, with no third-party trademarks, copyrighted music or other material without permission. This is its script, and how to record it.

## Before recording

1. **The stack on its platform.** The Azure deployment ([deploy/azure](deploy/azure/README.md)): the dashboard at `https://<fqdn>/` signed in as `judge`, the analysis room at `https://<fqdn>:8443/analysis/`, so the Assistant and the Match story are the team of agents on Azure OpenAI. Start from clean data: `ssh p4n4@<ip> bh-albion/iot/scripts/reset_demo.sh`, then two minutes for the past matches.
2. **Time it to the live match.** `python3 iot/scripts/simulate.py --schedule` prints when the live match reaches each moment in local time. Start recording at about **33'** (the line drops and the deviations open at 35'–40'); the half-time report lands a minute after the whistle, and #7's legs go at 65'–70'. If you miss a moment, the next match tells the same story 44 minutes later. Record each shot separately and cut them together; the timings below are the edit's, not the match's.
3. **The tabs.** The dashboard (Home, Assistant) signed in as **judge** (the analyst's view); the analysis room (`:8443/analysis/`, signed in); the inbox (`:8443/mail/`); the Azure portal on the resource group (the VM, Key Vault, Azure OpenAI, the Azure ML workspace), and Azure ML Studio on the shirt-number run's metrics.
4. **The screen.** 1920×1080, dark mode, browser zoom 110%, notifications off. No music, or music you have the rights to. The Premier League's marks and the clubs' crests are on screen: confirm with the organisers that the category allows them before uploading (README, p4n4-dashboard, Marks).

## The script

| Time | On screen | Voice over |
|---|---|---|
| 0:00–0:10 | The analysis room, the live match at about 35': the score bug, the game plan's scorecard, the gradient across the top | "Football stories don't start and end with the scoreline. This is Match Intelligence: a club's match device measures every match every five minutes, against the staff's game plan." |
| 0:10–0:28 | The scorecard turns: *Defensive line height* ✕, *Team length* ✕. Open the deviation: what was measured, against the plan, what to check. Press **F**, type *"Back four not stepping up: talk at the break"* | "It doesn't just report that something happened, it explains it: from 35 minutes the line dropped to 34.8 metres against the 38 the plan asked for. The analyst flags it for the bench, and it's on the bench's phones." |
| 0:28–0:42 | Team data: pass length and difficulty, hard passes completed, pass speed and the fastest shot. Then Running: the bars, a ⚡ 31 km/h milestone, #7 Tomas Calloway's drop | "Pass quality, ball and shot speed, and every player's running, with milestones as they happen. When a player's legs go, it names him, as a reading for the staff, never a verdict." |
| 0:42–1:08 | The dashboard's Assistant: ask *"How are we doing, and what should we tell the players?"*. The steps appear: *the analyst · match summary*, *the coach's brief · briefing*; then the answer | "Behind the assistant is a team of AI agents on Microsoft Agent Framework and Azure OpenAI. An orchestrator calls an analyst for the numbers and a coach for the options, at the same time, and every number comes from the match device, never from the model." |
| 1:08–1:30 | The analysis room's Match story: *A fan*, *Español*, *Follow #7*. Tell the story. Then *Broadcast captions*; then *العربية* (right to left) | "The narrator agent turns the same numbers into the story each audience needs: for a fan, in Spanish, following their favourite player; as captions for a broadcast's lower third; in fourteen languages." |
| 1:30–1:45 | Mailpit: the half-time report, the narrator's story of the half first, then the plan's targets and the deviations with the analyst's note | "At half time, the report is on the coaches' phones before they reach the dressing room, with the story of the half." |
| 1:45–1:55 | The Azure portal: the resource group (the VM, Key Vault, Azure OpenAI); Azure ML Studio: the shirt-number model's run, its read and invented rates per epoch | "It runs on Azure: the club's device on a VM, its secrets in Key Vault, Azure OpenAI by managed identity, and our own shirt-number model trained on Azure Machine Learning. With no network, it falls back to a local model." |
| 1:55–2:00 | The logo, *Match Intelligence*, the repository's URL | "Every match, made for every fan." |

**Keep it under 2:00:** judges needn't watch beyond two minutes. Cut the Azure portal shot first if it runs long.

## Questions judges may ask

- **"Is this real Premier League data?"** No: every match is synthetic, played by a match engine 5 times a second and marked *Demo data* on every screen. The clubs and their crests are the league's; the players' names are made up.
- **"What do the agents actually do?"** The orchestrator reads the question and the view asking, and calls the specialists it needs as tools (Agent Framework's agents as tools); the analyst, coach and narrator call the ingest service's tools. A function middleware streams each call to the chat. Without Azure, one local model on Ollama does it all, offline.
- **"Can a model invent a number?"** The tools return the match device's numbers and sentences; the prompts tell every agent to repeat them exactly, and the recaps are written from facts gathered before the narrator starts. The tests check that the answer carries the ingest service's own numbers.
- **"Why train your own number model?"** The vision-language model takes seconds a crop on CPU; ours takes milliseconds, so every player can be read, not only the alerts'. It's trained on synthetic players in every kit, including the shirts no one could read, labelled *none*, so it learns not to guess. It replaces the vision-language model only if it beats it on our own filmed footage.
- **"How would it reach fans?"** The same recap endpoint takes an audience, a language and a player to follow; MQTT carries every window, play and deviation, timed by match minute, for anything that draws over a broadcast.

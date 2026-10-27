## Pasiero Futbol · match intelligence

The club's match device measures every match every 5 minutes: both teams' data (the press: PPDA, regains, the counter-press; pass quality and ball speed), their shape in and out of possession, the **set pieces** and **every player's running**. The staff's **game plan** sets the targets; when one is off the plan for 15 minutes, a **deviation** comes in for the analyst to flag for the bench or set aside as expected. A **team of AI agents** (Microsoft Agent Framework, on Azure OpenAI or the device's own model) answers from those numbers and tells the match's story for each audience, in any language.

- [Deviations and key plays](tab:feed): flag or set aside, with each play's frame.
- [Analysis room](http://localhost:8090/analysis/): the plan target by target, the shape, the set pieces, every player's running, the timeline, every play replayed in 2D.
- [Match story](http://localhost:8090/analysis/#story-card): the narrator agent's story of the match, for a fan, the coaching staff, an analyst or broadcast captions, in 14 languages, following one player if you like.
- [Game plan](http://localhost:8090/analysis/plan.html): formations and targets, per match or the club's default.
- [Season](http://localhost:8090/analysis/season.html): our set-piece routines and theirs, the squad's running per 90.
- [Half-time report](http://localhost:8090/analysis/report.html?part=half) · [Match report](http://localhost:8090/analysis/report.html?part=full): ready to print.
- [Cameras](tab:video): the live match, from the tactical camera and the broadcast's · [Match and season statistics](tab:grafana)

Every match here is synthetic: a match engine plays Brighton & Hove Albion's 2026-27 season against the Premier League's clubs, and the same analytics measure it that would measure a real one. Players' names are made up.

Ask the assistant: [How are we doing?](ask:) · [What should we tell the players?](ask:) · [Who is running less than usual?](ask:) · [Tell the story of the match for a fan, in Spanish](ask:)

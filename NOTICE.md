# Notice

The code and documentation in this repository are under the MIT licence ([LICENSE](LICENSE)). These parts are not, and the licence gives no rights to them.

## Marks

- **The Premier League's** name, logo and lion: `brand-src/premier-league.svg`, `iot/ingest/analysis/marks/`, and the theme drawn from them (`theme/icon.png`, `theme/logo-*.png`, `theme/grafana/`, by `iot/scripts/theme_assets.py`).
- **The clubs'** names and crests: `brand-src/brighton-crest.svg`, `iot/ingest/analysis/crests/`, `iot/ingest/analysis/league.json`.

They are trademarks of the Football Association Premier League Limited and of the clubs. The files are Wikipedia's copies (each one's source page in [`brand-src/sources.json`](brand-src/sources.json)). They're used only to theme this hackathon entry for its Premier League category. The project is not affiliated with, sponsored by or endorsed by the Premier League or any club. To use this code for anything else, replace them: the files above, which `theme/brand.json`, `league.json` and the analysis room's pages point to.

Every match, player name and number in the data is made up (`"synthetic": true`). No real match, player or footage is in the repository.

## Fonts

- Barlow, Barlow Condensed and IBM Plex Mono (`theme/fonts/`, `iot/ingest/analysis/fonts/`): SIL Open Font License 1.1, each with its licence beside it.
- The shirt-number model's training fonts are downloaded when it trains, from [Google Fonts](https://github.com/google/fonts/tree/main/ofl) (`ai/vision/shirts/synth.py`). They are all SIL Open Font License, and none is in the repository.

## The vision pipeline

`ai/vision/` uses [Ultralytics](https://github.com/ultralytics/ultralytics) and YOLO weights (`fetch_models.py`: Ultralytics' own and two community models trained from them). These are under AGPL-3.0, and the ball model also carries SoccerNet's data terms. The weights are downloaded, not committed. If you distribute the vision image, or serve it to others, AGPL-3.0's terms apply to it. Nothing else in the stack depends on it, and our shirt-number model (`shirt_numbers.onnx`, ONNX Runtime) uses none of it.

## Images the stack runs

The Docker images the stacks pull (Grafana, InfluxDB, n8n, Mosquitto, Ollama and its models, Mailpit, go2rtc, Caddy, and p4n4's) come under their own licences. None is redistributed here.

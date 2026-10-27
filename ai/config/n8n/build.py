#!/usr/bin/env python3
"""Builds the n8n workflow files in workflows/ from the JavaScript in src/.

    python3 ai/config/n8n/build.py

Each Code node is the theme's BRAND (its name and its emails' colours, from
themes/<id>/theme.json for the theme in use), src/helpers.js, then its own file. n8n publishes the
version named by a workflow's versionId, so every build gives a changed
workflow a new one, derived from its content. Edit the sources and rebuild
rather than editing the JSON; after a rebuild, set N8N_REIMPORT_WORKFLOWS=true
for one start to load it.

    deviations  POST /webhook/brighton/deviation   (the ingest service, a deviation opening during a match)
                → the staff: email, plus Slack and Telegram if set
    reports     POST /webhook/brighton/half-time   (the ingest service, at half time)
                POST /webhook/brighton/full-time   (the ingest service, at full time)
                GET  /webhook/brighton/report?match_id=…&part=half|full   (now)
                → the half-time report to the staff, the match report to the coaching staff, with a CSV and the
                  narrator agent's story of the half or the match (the agent's POST /api/recap, RECAP_LANGUAGE)
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
SRC = HERE / "src"
OUT = HERE / "workflows"
NS = uuid.UUID("5d3c6a2e-8b1f-4c7d-a1e9-2f6a17ffcd00")
SMTP = {"smtp": {"id": "brightonSmtp", "name": "Brighton analysis SMTP"}}


def code(*names: str) -> str:
    return "\n".join((SRC / f"{n}.js").read_text().rstrip("\n") + "\n" for n in names)


def brand() -> str:
    """The theme in use's name and emails' colours (iot/scripts/theme.py), as the BRAND helpers.js draws with."""
    theme = json.loads((ROOT / "theme" / "brand.json").read_text())["id"]
    spec = json.loads((ROOT / "themes" / theme / "theme.json").read_text())
    value = {"name": spec["name"], **spec["email"], "bar": [c.split()[0] for c in spec["bar"]]}
    return f"// The theme ({theme}, themes/{theme}/theme.json)\nconst BRAND = {json.dumps(value, ensure_ascii=False)};\n"


def node(id_: str, name: str, type_: str, version, position, parameters: dict, **extra) -> dict:
    return {"parameters": parameters, "id": id_, "name": name, "type": f"n8n-nodes-base.{type_}",
            "typeVersion": version, "position": position, **extra}


def js(id_: str, name: str, position, *sources: str) -> dict:
    return node(id_, name, "code", 2, position, {"jsCode": brand() + "\n" + code("helpers", *sources)})


def webhook(id_: str, name: str, position, path: str, method: str, mode: str) -> dict:
    return node(id_, name, "webhook", 2, position,
                {"path": path, "httpMethod": method, "responseMode": mode, "options": {}},
                webhookId=str(uuid.uuid5(NS, f"{path}|{method}")))


def ingest(id_: str, name: str, position, path: str) -> dict:
    """A GET to the ingest service's agent API, with the read token. `path` is an n8n expression body."""
    return node(id_, name, "httpRequest", 4.2, position, {
        "method": "GET",
        "url": "={{ $env.INGEST_URL }}" + path,
        "sendHeaders": True,
        "headerParameters": {"parameters": [{"name": "Authorization", "value": "=Bearer {{ $env.INGEST_READ_TOKEN }}"}]},
        "options": {},
    })


def env_set(id_: str, name: str, position, variable: str) -> dict:
    return node(id_, name, "if", 2, position, {
        "conditions": {
            "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose"},
            "conditions": [{"id": f"{id_}-c", "leftValue": "={{ $env." + variable + " }}", "rightValue": "",
                            "operator": {"type": "string", "operation": "notEmpty", "singleValue": True}}],
            "combinator": "and",
        },
        "options": {},
    })


def note(id_: str, position, width: int, height: int, content: str) -> dict:
    return node(id_, "About", "stickyNote", 1, position, {"width": width, "height": height, "content": content})


def chain(*names: str) -> dict:
    return {a: {"main": [[{"node": b, "type": "main", "index": 0}]]} for a, b in zip(names, names[1:])}


def workflow(id_: str, name: str, description: str, nodes: list[dict], connections: dict) -> dict:
    wf = {"id": id_, "name": name, "active": False, "isArchived": False, "nodes": nodes, "connections": connections,
          "settings": {"executionOrder": "v1", "saveDataSuccessExecution": "all"},
          "meta": {"templateCredsSetupCompleted": True}, "pinData": {}, "tags": [], "description": description}
    wf["versionId"] = str(uuid.uuid5(NS, json.dumps(wf, sort_keys=True)))
    return wf


def deviations() -> dict:
    nodes = [
        webhook("d-hook", "Deviation", [0, 100], "brighton/deviation", "POST", "onReceived"),
        js("d-message", "Message", [240, 100], "deviation.message"),
        node("d-email", "Email the staff", "emailSend", 2.1, [500, -40], {
            "fromEmail": "={{ $env.EMAIL_FROM }}", "toEmail": "={{ $json.to }}", "subject": "={{ $json.subject }}",
            "emailFormat": "both", "text": "={{ $json.text }}", "html": "={{ $json.html }}",
            "options": {"appendAttribution": False},
        }, credentials=SMTP),
        env_set("d-slack-if", "Slack set?", [500, 120], "SLACK_WEBHOOK_URL"),
        node("d-slack", "Post to Slack", "httpRequest", 4.2, [720, 100], {
            "method": "POST", "url": "={{ $env.SLACK_WEBHOOK_URL }}", "sendBody": True, "contentType": "json",
            "specifyBody": "json", "jsonBody": "={{ JSON.stringify({ text: '*' + $json.subject + '*\\n' + $json.text }) }}",
            "options": {},
        }, onError="continueRegularOutput"),
        env_set("d-telegram-if", "Telegram set?", [500, 280], "TELEGRAM_BOT_TOKEN"),
        node("d-telegram", "Send to Telegram", "httpRequest", 4.2, [720, 260], {
            "method": "POST", "url": "=https://api.telegram.org/bot{{ $env.TELEGRAM_BOT_TOKEN }}/sendMessage",
            "sendBody": True, "contentType": "json", "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({ chat_id: $env.TELEGRAM_CHAT_ID, text: $json.subject + '\\n\\n' + $json.text }) }}",
            "options": {},
        }, onError="continueRegularOutput"),
        note("d-note", [0, -260], 560, 220,
             "## Brighton · off the plan\nThe ingest service posts every deviation from the game plan that opens during a match "
             "to `POST /webhook/brighton/deviation` (with `X-Notify-Token`). Those whose priority is in `NOTIFY_PRIORITY` "
             "(default `high`: the plan's key instructions) are emailed to `STAFF_EMAIL_TO`, and posted to Slack and "
             "Telegram when those are set (the bench's phones), with a link that opens the deviation in the analysis room.\n\n"
             "The code lives in `ai/config/n8n/src/`; settings are in `ai/.env`."),
    ]
    connections = {
        **chain("Deviation", "Message"),
        **chain("Slack set?", "Post to Slack"),
        **chain("Telegram set?", "Send to Telegram"),
        "Message": {"main": [[{"node": n, "type": "main", "index": 0}
                              for n in ("Email the staff", "Slack set?", "Telegram set?")]]},
    }
    return workflow("brightonDeviations", "Brighton · off the plan",
                    "Tells the staff when a key target of the game plan goes off plan during a match.", nodes, connections)


def reports() -> dict:
    nodes = [
        webhook("r-half", "Half time", [0, -80], "brighton/half-time", "POST", "onReceived"),
        webhook("r-whistle", "Full time", [0, 80], "brighton/full-time", "POST", "onReceived"),
        js("r-which-whistle", "Which match", [220, 0], "report.whistle"),
        node("r-wait", "Let the analyst finish", "wait", 1.1, [440, 0],
             {"amount": "={{ $json.delay }}", "unit": "seconds"}, webhookId=str(uuid.uuid5(NS, "report-wait"))),
        webhook("r-now", "Report now", [0, 260], "brighton/report", "GET", "lastNode"),
        js("r-manual", "Match asked for", [440, 260], "report.manual"),
        node("r-which", "Which report", "noOp", 1, [660, 100], {}),
        ingest("r-match", "Read match", [880, 100],
               "/api/v1/agent/match?match_id={{ encodeURIComponent($json.match_id) }}&site_id={{ encodeURIComponent($json.site_id) }}"),
        ingest("r-plan", "Read plan", [1100, 100],
               "/api/v1/agent/plan?match_id={{ encodeURIComponent($json.match.match_id) }}&site_id={{ encodeURIComponent($json.match.site_id) }}"),
        # The narrator agent's story of the half or the match; a report goes out without it if the agent can't write one
        node("r-story", "Narrator's story", "httpRequest", 4.2, [1320, 100], {
            "method": "POST", "url": "={{ $env.AGENT_URL }}/api/recap", "sendBody": True, "contentType": "json",
            "specifyBody": "json",
            "jsonBody": "={{ JSON.stringify({ match_id: $('Read match').first().json.match.match_id, "
                        "part: $('Which report').first().json.part === 'half' ? 'half' : 'full', "
                        "audience: $env.RECAP_AUDIENCE || 'coach', language: $env.RECAP_LANGUAGE || 'en' }) }}",
            "options": {"timeout": 240000},
        }, onError="continueRegularOutput"),
        js("r-build", "Build report", [1540, 100], "report.build"),
        node("r-email", "Email the staff", "emailSend", 2.1, [1760, 100], {
            "fromEmail": "={{ $env.EMAIL_FROM }}", "toEmail": "={{ $json.to }}", "subject": "={{ $json.subject }}",
            "emailFormat": "html", "html": "={{ $json.html }}",
            "options": {"appendAttribution": False, "attachments": "csv"},
        }, credentials=SMTP),
        node("r-result", "Result", "code", 2, [1980, 100], {"jsCode": (
            "// The webhook's response: what was sent\nconst r = $('Build report').first().json;\n"
            "return [{ json: { match_id: r.match_id, part: r.part, score: r.score, subject: r.subject, to: r.to,\n"
            "    on_plan_share: r.on_plan_share, deviations: r.deviations, file: r.file } }];\n")}),
        note("r-note", [220, -380], 680, 260,
             "## Brighton · reports\nAt half time the ingest service posts `POST /webhook/brighton/half-time`; `HALFTIME_DELAY` "
             "seconds later (default 60, so the analyst can make the last calls) the half-time report goes to `STAFF_EMAIL_TO`: "
             "the plan's targets in the first half, the deviations with the analyst's calls and notes, the numbers and the key "
             "plays, the narrator agent's story of the half (`RECAP_LANGUAGE`, `RECAP_AUDIENCE`), with a button to the "
             "printable report. At full time (`/full-time`), `REPORT_DELAY` seconds later "
             "(default 300), the match report goes to `REPORT_EMAIL_TO`. Both attach a CSV of every target in every 5 minutes.\n\n"
             "`GET /webhook/brighton/report?match_id=…&part=half|full` sends one now (default: the match being played, or the latest)."),
    ]
    connections = {
        **chain("Half time", "Which match"),
        **chain("Full time", "Which match", "Let the analyst finish", "Which report", "Read match", "Read plan",
                "Narrator's story", "Build report", "Email the staff", "Result"),
        **chain("Report now", "Match asked for", "Which report"),
    }
    return workflow("brightonReports", "Brighton · reports",
                    "Emails the staff the half-time report and the coaching staff the match report.", nodes, connections)


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for name, wf in (("deviations", deviations()), ("reports", reports())):
        path = OUT / f"{name}.json"
        text = json.dumps(wf, indent=2, ensure_ascii=False) + "\n"
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
            print(f"wrote {path.relative_to(HERE)} ({wf['versionId']})")


if __name__ == "__main__":
    main()

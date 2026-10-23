"""
Offline checks for the team of agents (ai/agent/team.py, Microsoft Agent Framework) against a real ingest service
(in-process, on a free port) holding one of the demo season's past matches. A scripted chat client stands in for
Azure OpenAI, so what's checked is the team's wiring: the orchestrator delegating to its specialists, their tools
calling the ingest service, the steps and the answer streaming as Ollama chunks, the fallback to Ollama, and the
recaps. No Docker, no model, no network.

    pip install -r ai/agent/requirements.txt    # the framework (the agent's image has it)
    python3 tests/test_team.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import timezone
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT / "iot/ingest"), str(ROOT / "iot/scripts"), str(ROOT / "ai/agent")]

try:
    from agent_framework import (BaseChatClient, ChatResponse, ChatResponseUpdate, Content, FunctionInvocationLayer,
                                 Message)
except ImportError:
    print("== skipped: Microsoft Agent Framework isn't installed (pip install -r ai/agent/requirements.txt)")
    sys.exit(0)

import ingest  # noqa: E402
import plan as rules  # noqa: E402
import simulate  # noqa: E402
from store import Store  # noqa: E402

failed = False


def check(label: str, ok: bool, detail: object = "") -> None:
    global failed
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + ("" if ok else f": {str(detail)[:400]}"))
    failed |= not ok


# ── a real ingest service with one past match ────────────────────────────────
tmp = tempfile.TemporaryDirectory()
store = Store(f"{tmp.name}/team.db", timezone.utc, 400, "2026-03-07")
service = ingest.Service(store, {"site-token": simulate.SITE}, "read-token", simulate.SITE)
service.influx.url = service.mqtt.host = service.notify.url = ""
server = ThreadingHTTPServer(("127.0.0.1", 0), ingest.handler(service))
threading.Thread(target=server.serve_forever, daemon=True).start()
os.environ.update(INGEST_URL=f"http://127.0.0.1:{server.server_port}", INGEST_READ_TOKEN="read-token",
                  AZURE_OPENAI_ENDPOINT="http://127.0.0.1:9/", AZURE_OPENAI_DEPLOYMENT="test-model",
                  AZURE_OPENAI_API_KEY="test-key", AGENT_BACKEND="auto", OLLAMA_URL="http://127.0.0.1:9")

match = simulate.history(timezone.utc, time.time())[-1]
service.ingest("/api/v1/fixtures", match.fixture(time.time()), simulate.SITE)
store.put_plan(simulate.SITE, match.match_id, rules.check(match.plan or simulate.DEFAULT_PLAN), "coach")
service.ingest("/api/v1/windows", match.window_docs(time.time()), simulate.SITE)
service.ingest("/api/v1/plays", match.play_docs(time.time()), simulate.SITE)
score = store.match_info(simulate.SITE, store._fixture(simulate.SITE, match.match_id))
score = f"{score['home']['code']} {score['score']['home']}-{score['score']['away']} {score['away']['code']}"

import agent  # noqa: E402

agent.load_local_tools()
agent.load_team()
team = agent.TEAM

print("== the team")
check("team.py loads, enabled on Azure OpenAI", team is not None and team.enabled())


class Scripted(FunctionInvocationLayer, BaseChatClient):
    """
    Stands in for Azure OpenAI. The orchestrator (it is offered the specialists) asks the analyst and the coach at
    once, then answers with what they said; a specialist calls its first tool, then reports what it returned; with no
    tools (a recap), it writes the task back. Every request is kept, to check what each agent was asked.
    """

    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.fail, self.seen = fail, []

    async def _inner_get_response(self, *, messages, stream, options, **kwargs):
        if self.fail:
            raise ConnectionError("Azure OpenAI is unreachable")
        offered = [t.name for t in options.get("tools") or []]
        results = [c for m in messages for c in m.contents if c.type == "function_result"]
        who = ("orchestrator" if "analyst" in offered else "coach" if "get_briefing" in offered
               else "analyst" if "get_device_status" in offered else "narrator" if offered else "recap")
        self.seen.append((who, offered, [m.text for m in messages]))
        if who == "orchestrator" and not results:
            contents = [Content.from_function_call("a1", "analyst", arguments={"task": "How are we doing? (staff, English)"}),
                        Content.from_function_call("c1", "coach", arguments={"task": "What should we tell the players?"})]
        elif who in ("analyst", "coach") and not results:
            name = "get_briefing" if who == "coach" else "get_match_summary"
            contents = [Content.from_function_call(f"{who}-t", name, arguments={})]
        elif who == "recap":
            contents = [Content.from_text("RECAP " + messages[-1].text[:120])]
        else:
            said = " | ".join(str(c.result if not isinstance(c.result, list) else c.result[0].text)[:300] for c in results)
            contents = [Content.from_text(f"- {who}: " + said)]
        if stream:
            async def updates():
                for c in contents:
                    yield ChatResponseUpdate(role="assistant", contents=[c])
            return self._build_response_stream(updates())
        return ChatResponse(messages=[Message(role="assistant", contents=contents)])


chunks: list[dict] = []
client = Scripted()
question = {"model": "gemma4:e2b", "stream": True, "messages": [
    {"role": "system", "content": "The question comes from the power user view."},
    {"role": "user", "content": "How are we doing, and what should we tell the players?"}]}
result = team.chat(question, chunks.append, client=client)
steps = [(c["step"]["tool"], c["step"]["state"]) for c in chunks if "step" in c]
check("the orchestrator asks the analyst and the coach", ("analyst", "running") in steps and ("coach", "running") in steps, steps)
check("…which call their tools: the match summary, the briefing",
      ("get_match_summary", "done") in steps and ("get_briefing", "done") in steps, steps)
labels = [c["step"]["label"].get("en") for c in chunks if "step" in c and c["step"]["tool"] == "get_match_summary"]
check("a specialist's tool is named with the specialist in the chat", labels and labels[0].startswith("the analyst · "), labels)
check("every step runs, then is done and ok", all(c["step"]["ok"] for c in chunks if c.get("step", {}).get("state") == "done"), steps)
text = "".join(c["message"]["content"] for c in chunks if "step" not in c and not c.get("done"))
check("the answer streams, with the ingest service's own numbers", score in text, text[:300])
final = chunks[-1]
check("the last chunk is done, with the questions to ask next", final["done"] and final.get("suggestions"), final)
check("it returns the whole reply", result["message"]["content"] == text and result["done"], result["message"]["content"][:80])
check("Ollama's chunk shape, so the dashboard reads it", all({"model", "message", "done"} <= set(c) for c in chunks))
orchestrator = next(s for s in client.seen if s[0] == "orchestrator")
check("the orchestrator sees the view and the time in a context block", "<context>" in orchestrator[2][-1]
      and "power user view" in orchestrator[2][-1], orchestrator[2][-1][:200])
check("…and only the three specialists as tools", sorted(orchestrator[1]) == ["analyst", "coach", "narrator"], orchestrator[1])
analyst = next(s for s in client.seen if s[0] == "analyst")
check("the analyst gets the orchestrator's task, not the whole chat", any("How are we doing? (staff, English)" in t for t in analyst[2]), analyst[2])
check("the analyst's tools are the numbers, never the briefing", "get_briefing" not in analyst[1] and "get_game_plan" in analyst[1], analyst[1])

print("== the fallback")
broken = Scripted(fail=True)
try:
    team.chat(question, lambda c: None, client=broken)
    check("Azure failing before any text: Unavailable", False, "no exception")
except team.Unavailable as exc:
    check("Azure failing before any text: Unavailable", "unreachable" in str(exc), exc)
ollama_calls: list[dict] = []
original_chat = agent.agent_chat
agent.agent_chat = lambda body, emit: ollama_calls.append(body) or {"message": {"role": "assistant", "content": "on Ollama"}}
team_chat = team.chat
team.chat = lambda body, emit, client=None: team_chat(body, emit, client=broken)
check("agent.py then answers on Ollama", agent.answer(question, lambda c: None)["message"]["content"] == "on Ollama" and ollama_calls)
team.chat = team_chat
agent.agent_chat = original_chat
saved = team.BACKEND
team.BACKEND = "ollama"
check("AGENT_BACKEND=ollama turns the team off", not team.enabled())
team.BACKEND = saved

print("== recaps")
for bad, why in (({"audience": "everyone"}, "audience"), ({"part": "extra"}, "part"), ({"language": "es; drop"}, "language"),
                 ({"focus": "seven"}, "focus"), ({"focus": 120}, "focus")):
    try:
        team.recap(bad, client=client)
        check(f"a bad {why} is refused", False, bad)
    except ValueError as exc:
        check(f"a bad {why} is refused", why in str(exc), exc)
r = team.recap({"audience": "fan", "language": "es"}, client=client)
check("a fan's recap, in Spanish, of the match", r["recap"].startswith("RECAP Write a recap for a casual fan")
      and r["language"] == "Spanish" and r["backend"] == "azure" and r["match_id"] == match.match_id, r)
prompt = next(s for s in reversed(client.seen) if s[0] == "recap")[2][-1]
check("the narrator gets the facts: the score, the plan, the key plays", score in prompt and '"game_plan"' in prompt
      and '"key_plays"' in prompt and "in Spanish" in prompt, prompt[:300])
check("the second time, from the cache", team.recap({"audience": "fan", "language": "Spanish"}, client=client)["cached"])
seven = team.facts(None, "full", 7)
check("following one player: his running and the plays that name him", seven["focus"]["player"]["number"] == 7
      and all("#7" in p["summary"] for p in seven["focus"]["plays"]), seven.get("focus"))
try:
    team.facts(None, "full", 99)
    check("a player who didn't play is refused", False)
except ValueError as exc:
    check("a player who didn't play is refused", "#99" in str(exc), exc)
half = team.facts(None, "half", None)
check("the first half's recap has only its key plays", half["key_plays"] and all(
    int(p["minute"].split("+")[0]) <= 45 for p in half["key_plays"]), [p["minute"] for p in half["key_plays"]])
captions = team.task(half, "half", "broadcast", "English", None)
check("broadcast captions: short lines for a lower third", "lower third" in captions and "under 80 characters" in captions)
original_ollama = agent.ollama_chat
agent.ollama_chat = lambda request: iter([{"message": {"content": "Resumen "}}, {"message": {"content": "en Ollama."}}])
r = team.recap({"audience": "coach", "language": "es"}, client=Scripted(fail=True))
check("Azure failing: the recap is Ollama's", r["recap"] == "Resumen en Ollama." and r["backend"] == "ollama", r)

print("== over HTTP")
http = ThreadingHTTPServer(("127.0.0.1", 0), agent.Handler)
threading.Thread(target=http.serve_forever, daemon=True).start()


def post(path: str, body: dict) -> tuple[int, dict]:
    request = urllib.request.Request(f"http://127.0.0.1:{http.server_port}{path}", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


status, body = post("/api/recap", {"audience": "nobody"})
check("POST /api/recap refuses bad parameters: 400", status == 400 and "audience" in body["error"], (status, body))
status, body = post("/api/recap", {"audience": "analyst", "language": "pt"})
check("POST /api/recap: Azure unreachable (a closed port here), so Ollama's", status == 200 and body["backend"] == "ollama"
      and body["language"] == "Portuguese", (status, body))
agent.ollama_chat = original_ollama
http.shutdown()

sys.exit(1 if failed else 0)

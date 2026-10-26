"""
The match assistant as a team of agents, on Microsoft Agent Framework and Azure OpenAI (agent.py loads this file).

p4n4-dashboard's Assistant tab asks one assistant; behind it, four agents work together:

    orchestrator ──► analyst    the numbers: the match, the plan, the shapes, the plays, set pieces, running, the
                │               season, the match device
                ├──► coach      what the staff could talk about: the plan's own instruction for each target off it,
                │               the running drops, what's going well, the analyst's calls (options, never orders)
                └──► narrator   the story: why a moment matters, recaps for a fan, a coach, an analyst or a
                                broadcast's captions, in any language, following one player if asked

The orchestrator calls the specialists as tools (Agent.as_tool), as many as the question needs, and answers in the
question's language and the viewer's register. The specialists call local_tools.py's tools, which call the ingest
service: no agent counts anything. A function middleware streams each step to the chat as agent.py's loop does
(`step`), so the Assistant tab shows "the analyst · match summary" while the team works.

POST /api/recap (recap below) has the narrator tell a match's story: the facts are gathered here, by the same tools,
then told for the audience, in the language asked, about one of our players if asked. On Azure, the narrator agent
writes it; without Azure, Ollama's model does, so recaps work offline too.

Without Azure OpenAI, or without the framework's packages, agent.py answers alone on Ollama, as before: the fallback
that runs on the club's device with no network. A question that fails on Azure before any text has streamed goes to
Ollama too. AGENT_BACKEND=local runs the same team on the device instead, on Ollama's model through its
OpenAI-compatible API: offline, the narrator agent writing the recaps too.

    AGENT_BACKEND             auto (default: azure when AZURE_OPENAI_ENDPOINT and _DEPLOYMENT are set), azure,
                              local (the team on Ollama's model), ollama (agent.py alone)
    AZURE_OPENAI_ENDPOINT     https://<resource>.openai.azure.com/
    AZURE_OPENAI_DEPLOYMENT   the chat model's deployment name, e.g. gpt-4.1-mini
    AZURE_OPENAI_API_KEY      a key; empty: Microsoft Entra ID (a managed identity on Azure, `az login` on a laptop)
    AZURE_OPENAI_API_VERSION  optional: the API version (the framework's default otherwise)
    OLLAMA_MODEL              the fallback's model, for recaps, and the local team's (default: agent.py's AGENT_WARM_MODEL)
    OLLAMA_URL                Ollama, for the local team (default http://ollama:11434)
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime

BACKEND = os.environ.get("AGENT_BACKEND", "auto").strip().lower() or "auto"
ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT", "").strip()
DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "").strip()
API_KEY = os.environ.get("AZURE_OPENAI_API_KEY", "").strip()
API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "").strip()
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://ollama:11434").strip().rstrip("/")

# Which of local_tools.py's tools each specialist gets (the orchestrator gets the specialists)
SPECIALISTS = {
    "analyst": ("get_match_summary", "get_game_plan", "get_formation", "list_plays", "get_play", "get_set_pieces",
                "get_running", "get_season_summary", "get_device_status"),
    "coach": ("get_briefing", "get_running", "get_game_plan"),
    "narrator": ("get_match_summary", "list_plays", "get_play", "get_running", "get_set_pieces"),
}
# How the chat names a specialist while it works, per language (agent.py's LABELS name the tools)
LABELS = {
    "analyst": {"en": "the analyst", "es": "el analista"},
    "coach": {"en": "the coach's brief", "es": "el resumen para el cuerpo técnico"},
    "narrator": {"en": "the narrator", "es": "el narrador"},
}

ORCHESTRATOR = """\
You are Match Intelligence, the match assistant of Brighton & Hove Albion's coaching staff (BHA, Premier League). \
The club's match device measures every match every 5 minutes against the staff's game plan: the press, the shape, \
set pieces, pass quality, every player's running. You lead three specialists, each a tool you call with a task in \
plain words:

- analyst: every number. The match being played or another (by opponent), the score, the game plan and its \
targets, the deviations and the analyst's calls, shapes, plays, set pieces, running, the season, the match device.
- coach: what the staff could talk about. A team talk, half time, what to change, who is tiring, who could come \
off: the plan's own instructions for what is off it, as options.
- narrator: the story. Why a moment mattered, a recap, explaining the match to a fan, captions for the broadcast, \
following one player.

Always call at least one specialist before answering about a match, a player, a play, the plan or the season; call \
every one the question needs, in the same turn when they don't depend on each other (how are we doing and what to \
tell the players: analyst and coach). Give each a self-contained task: the question, the match or opponent if one \
is named, who is asking (the coaching staff or an analyst) and the language to answer in. Questions are about the \
match being played unless they name another; never ask which match.

The latest question starts with a <context> block the system adds: the time, which dashboard view is asking, the \
system's status. Use it, never mention it.

Who is asking:
- The viewer view is the coaching staff, on a tablet: 2 to 4 short bullets in football language, the score and \
minute first if the match is on, then what matters. No ids, no tool or field names.
- The power user and admin views are the analysts: the same, plus the numbers behind it, the minutes and play ids \
where they help.
- No view given: as for the coaching staff.

How to answer:
- Use only what the specialists said. Keep their numbers, units, minutes and names exactly; never work out your own.
- Players are shirt numbers, with the name when a specialist gives one (#7 Tomas Calloway; the demo's names are \
made up). Never guess a name. "We", "us" and "our" are Brighton.
- Plain text for a chat bubble: short bullets starting with "-", bold for the one thing that matters most. No \
tables, no headings, no LaTeX.
- Options, never decisions: never tell the staff what to do, never pick a substitute, never judge a player or a \
coach. A running drop is a reading to set beside what the staff see.
- When the data is from the demo season, end with "(Demo data.)" once, in the answer's language.
- Answer what was asked, then stop: never offer to look anything else up (the app suggests the next questions).
- Answer in the language of the question."""

ANALYST = """\
You are the analyst of Brighton & Hove Albion's match intelligence team (BHA, Premier League). You answer a task \
from the team's lead with the numbers, from your tools only: the match device measures every match every 5 \
minutes (the press: PPDA, high regains, the counter-press; the shape; set pieces; pass quality; every player's \
running), and a deviation opens when one of the game plan's targets is off over the last 15 minutes.

Always call a tool; questions are about the match being played unless the task names a match or an opponent. \
Pick the tool:
- how we are doing, the score, what's happening, what the analyst flagged, what's still to call: get_match_summary
- the plan, a target, what's off plan, the line, the press (target press): get_game_plan
- shape, formation, positions: get_formation
- a list of plays: list_plays; one play: get_play with its type ("our last goal": type goal)
- set pieces, corners, throws, first contacts: get_set_pieces (period for the season, opponent for one club's)
- running, sprints, top speed, who is tiring: get_running (every_player for every player's numbers)
- the season, a month, which targets slip: get_season_summary
- the match device, its cameras, the tracking: get_device_status

Reply to the lead in short plain bullets: the facts the task needs, numbers exactly as the tools give them with \
their units, minutes as the tools write them (38', 45+2'), players as the tools name them, play ids when they \
help, and whether the data is synthetic (the demo season). Never judge a player or a coach. Write in English: \
the lead translates."""

COACH = """\
You prepare what Brighton & Hove Albion's coaching staff could talk about (BHA, Premier League): a team talk, half \
time, what to change, who is tiring. Call get_briefing first (get_running or get_game_plan only if the task needs \
more). Reply to the team's lead in short plain bullets: each option exactly as the tool words it (what was measured \
against the plan, and the plan's own instruction), the biggest running drops, the key targets going well, and the \
analyst's calls if they matter. These are options for the staff to weigh, never decisions: never tell them what to \
do, never pick a substitute, never judge a player or a coach. Say whether the data is synthetic (the demo season). \
Write in English: the lead translates."""

NARRATOR = """\
You are the narrator of Premier League Match Intelligence: you tell a match's story from what its match device \
measured, and above all why each moment mattered, not only that it happened.

Explain with the numbers you are given: control against chaos (possession, passes and field tilt against turnovers, \
regains and counter-attacks), the press (PPDA: a lower number is a harder press; high regains), how the game plan \
held or slipped (the targets, the deviations and when they opened and closed), the moments that turned the match \
(goals and how they came: a high regain, a counter, a set piece), the set pieces (routines and first contacts) and \
the running (a player's legs going, top speeds, distance). Use only the facts given or your tools'; keep numbers, \
minutes and names exactly; never invent a moment. Players are shirt numbers with their names when given (#7 Tomas \
Calloway first, then the surname); never judge a player or a coach. "We" is never used: say the clubs' names.

Write entirely in the language asked (keep club codes, shirt numbers and units). Plain text, no headings or \
tables. When the data is from the demo season, end with "(Demo data.)" once, in that language."""

# What each audience gets from the narrator
AUDIENCES = {
    "fan": "a recap for a casual fan, about 150 words: a headline line, then the story in plain words (say what a "
           "number means the first time: PPDA is how hard a team pressed), the one moment that decided it and why",
    "coach": "a recap for the coaching staff, about 150 words: how the game plan held, where it slipped and when, "
             "what the deviations and the analyst's calls were, the running drops, as readings not verdicts",
    "analyst": "a recap for an analyst, about 200 words, dense: the numbers behind each phase, minutes, the plan's "
               "targets with their values, the deviations, set pieces, pass quality and the running",
    "broadcast": "3 to 5 on-screen captions for a broadcast's lower third, one per line, each under 80 characters, "
                 "the minute first (63' …): the moments that mattered and the number that explains each",
}
PARTS = {"live": "the match so far", "half": "the first half", "full": "the whole match"}
LANGUAGES = {"en": "English", "es": "Spanish", "pt": "Portuguese", "fr": "French", "de": "German", "it": "Italian",
             "nl": "Dutch", "ar": "Arabic", "ja": "Japanese", "ko": "Korean", "zh": "Chinese", "hi": "Hindi",
             "sw": "Swahili", "tr": "Turkish", "pl": "Polish", "id": "Indonesian", "th": "Thai", "vi": "Vietnamese"}
LANGUAGE_NAME = re.compile(r"^[A-Za-z][A-Za-z ()-]{1,30}$")
# Recaps kept: a finished match's as long as the process lives (LRU), a live one's for LIVE_TTL seconds
CACHE_SIZE, LIVE_TTL = 64, 120

AGENT = None  # agent.py, from setup()
RUN: contextvars.ContextVar[Run] = contextvars.ContextVar("run")
_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
_cache_lock = threading.Lock()
_credential = None


class Unavailable(Exception):
    """The team can't answer now (Azure unreachable, a bad key, no quota): agent.py answers on Ollama instead."""


class Run:
    """One chat request: where its chunks go, the tools its agents called, whether any text has streamed."""

    def __init__(self, emit, model: str) -> None:
        self.emit, self.model = emit, model
        self.called: list[str] = []
        self.streamed = False

    def chunk(self, content: str = "", done: bool = False, **extra) -> dict:
        return {"model": self.model, "created_at": datetime.now(UTC).isoformat(),
                "message": {"role": "assistant", "content": content}, "done": done, **extra}


def setup(agent) -> None:
    """agent.py's load_team: keep its module (tools, labels, Ollama) for the agents and the recaps."""
    global AGENT
    AGENT = agent
    if BACKEND not in ("auto", "azure", "local", "ollama"):
        raise ValueError(f"AGENT_BACKEND must be auto, azure, local or ollama, not {BACKEND!r}")
    if BACKEND == "local":
        import agent_framework  # noqa: F401
        agent.log(f"team: orchestrator, {', '.join(SPECIALISTS)} on Ollama {local_model()} at {OLLAMA_URL}, offline")
    elif enabled():
        import agent_framework  # noqa: F401  (the packages are there, or the team isn't loaded)
        agent.log(f"team: orchestrator, {', '.join(SPECIALISTS)} on Azure OpenAI {DEPLOYMENT} at {ENDPOINT}"
                  f" ({'key' if API_KEY else 'Entra ID'}); Ollama is the fallback")
    else:
        agent.log("team: Azure OpenAI isn't set (AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_DEPLOYMENT): "
                  "chat on Ollama, recaps by Ollama's model")


def enabled() -> bool:
    """Whether the team answers: on the local model, or Azure OpenAI configured and not turned off."""
    if BACKEND in ("ollama", "local"):
        return BACKEND == "local"
    return bool(ENDPOINT and DEPLOYMENT)


def local_model() -> str:
    return os.environ.get("OLLAMA_MODEL", "").strip() or (AGENT.WARM_MODEL if AGENT else "") or "gemma4:e2b"


# ── the agents ────────────────────────────────────────────────────────────────


def azure_client():
    """A chat client on the Azure OpenAI deployment: a key, or Entra ID (a managed identity on Azure)."""
    global _credential
    from agent_framework.openai import OpenAIChatCompletionClient

    kwargs = {"model": DEPLOYMENT, "azure_endpoint": ENDPOINT, **({"api_version": API_VERSION} if API_VERSION else {})}
    if API_KEY:
        return OpenAIChatCompletionClient(api_key=API_KEY, **kwargs)
    if _credential is None:
        from azure.identity import DefaultAzureCredential

        _credential = DefaultAzureCredential()
    return OpenAIChatCompletionClient(credential=_credential, **kwargs)


def local_client():
    """A chat client on Ollama's OpenAI-compatible API: the team on the device, with no network."""
    from agent_framework.openai import OpenAIChatCompletionClient

    return OpenAIChatCompletionClient(model=local_model(), base_url=f"{OLLAMA_URL}/v1", api_key="ollama")


def client_for_team():
    """The team's chat client: Ollama's model with AGENT_BACKEND=local, else Azure OpenAI."""
    return local_client() if BACKEND == "local" else azure_client()


def _failed(result) -> bool:
    """Whether a tool's result (the framework's contents, or text) is local_tools.py's {"error": …}."""
    items = result if isinstance(result, list) else [result]
    return any(str(getattr(item, "text", item) or "").startswith('{"error"') for item in items)


def middleware(who: str):
    """A function middleware for one agent: each tool call it makes is a step in the chat, running then done."""
    from agent_framework import FunctionMiddleware

    class Steps(FunctionMiddleware):
        async def process(self, context, call_next):
            run, name = RUN.get(None), context.function.name
            own = LABELS.get(name) or (AGENT.LABELS.get(name) if AGENT else None) or {}
            # A specialist's tool is named with the specialist: "the analyst · match summary"
            label = {lang: f"{LABELS[who][lang]} · {text}" for lang, text in own.items() if lang in LABELS.get(who, {})} \
                if who in LABELS else own
            step = {"tool": name, "agent": who, "label": label or own}
            if run:
                run.emit(run.chunk(step={**step, "state": "running"}))
            try:
                await call_next()
            finally:
                if run:
                    if name not in LABELS:
                        run.called.append(name)
                    run.emit(run.chunk(step={**step, "state": "done", "ok": not _failed(context.result)}))

    return Steps()


def function_tool(name: str):
    """One of local_tools.py's tools for an agent: its own description and JSON schema, its result as JSON text."""
    from agent_framework import FunctionTool

    definition = next(d for d in AGENT.TOOLS if d["function"]["name"] == name)["function"]
    fn = AGENT.TOOL_FUNCTIONS[name]

    def call(**arguments) -> str:
        try:
            result = fn(arguments)
        except AGENT.ToolError as exc:
            result = {"error": str(exc)}
        except Exception as exc:  # the ingest service down: tell the agent, keep going
            AGENT.log(f"tool {name} failed: {exc}")
            result = {"error": f"the lookup failed: {exc}"}
        return json.dumps(result, ensure_ascii=False)

    return FunctionTool(name=name, description=definition.get("description", ""), func=call,
                        input_model=definition.get("parameters") or {"type": "object", "properties": {}})


def build(client) -> tuple:
    """The team on one chat client: (orchestrator, {name: specialist})."""
    from agent_framework import Agent

    prompts = {"analyst": ANALYST, "coach": COACH, "narrator": NARRATOR}
    descriptions = {
        "analyst": "The numbers of a match, the plan, shapes, plays, set pieces, running, the season or the device.",
        "coach": "What the staff could talk about: the plan's options for what is off it, who is tiring.",
        "narrator": "The story: why moments mattered, a recap for a fan, coach or analyst, captions, one player's match.",
    }
    specialists = {
        name: Agent(client=client, name=name, description=descriptions[name], instructions=prompts[name],
                    tools=[function_tool(t) for t in tools if t in AGENT.TOOL_FUNCTIONS], middleware=[middleware(name)])
        for name, tools in SPECIALISTS.items()
    }
    orchestrator = Agent(
        client=client, name="match_intelligence", instructions=ORCHESTRATOR,
        tools=[agent.as_tool(arg_name="task", arg_description="The task, self-contained: the question, the match, "
                                                               "who asks and the language")
               for agent in specialists.values()],
        middleware=[middleware("match_intelligence")])
    return orchestrator, specialists


def messages_for(body: dict) -> list:
    """The conversation as the framework's messages: agent.py's context block on the latest question."""
    from agent_framework import Message

    return [Message(role=m["role"], contents=[m["content"]])
            for m in AGENT.with_context(body.get("messages") or [])
            if m.get("role") in ("user", "assistant") and (m.get("content") or "").strip()]


async def _chat(body: dict, run: Run, client) -> str:
    orchestrator, _ = build(client)
    stream = orchestrator.run(messages_for(body), stream=True)
    reply = ""
    async for update in stream:
        if update.text:
            run.streamed = True
            reply += update.text
            run.emit(run.chunk(update.text))
    final = await stream.get_final_response()
    return reply or final.text


def chat(body: dict, emit, client=None) -> dict:
    """
    agent.py's /api/chat on the team: streams the orchestrator's answer and its agents' steps as Ollama chunks, and
    returns the last chunk with the whole reply. Raises Unavailable when it fails before any text has streamed.
    """
    run = Run(emit, f"ollama/{local_model()}" if BACKEND == "local" and client is None else f"azure/{DEPLOYMENT or 'test'}")
    token = RUN.set(run)
    started = time.monotonic()
    try:
        reply = asyncio.run(_chat(body, run, client or client_for_team()))
    except Exception as exc:
        if not run.streamed:
            raise Unavailable(f"{type(exc).__name__}: {exc}") from exc
        raise AGENT.OllamaError(f"the team stopped mid-answer: {exc}") from exc
    finally:
        RUN.reset(token)
    AGENT.log(f"team answered in {time.monotonic() - started:.1f}s with {', '.join(run.called) or 'no tools'}")
    final = run.chunk(done=True, done_reason="stop")
    if suggestions := AGENT.follow_ups(body.get("messages") or [], run.called):
        final["suggestions"] = suggestions
    emit(final)
    return {**final, "message": {"role": "assistant", "content": reply}}


# ── recaps ────────────────────────────────────────────────────────────────────


def language_of(value: object) -> str:
    """A language as a model reads it: a code (es) or a name (Spanish, Brazilian Portuguese)."""
    text = str(value or "en").strip()
    if text.lower() in LANGUAGES:
        return LANGUAGES[text.lower()]
    if not LANGUAGE_NAME.match(text):
        raise ValueError("language must be a code such as es, or a name such as Spanish")
    return text


def options(params: dict) -> dict:
    """POST /api/recap's parameters, checked: match_id, part, audience, language, focus."""
    audience = str(params.get("audience") or "fan").strip().lower()
    if audience not in AUDIENCES:
        raise ValueError(f"audience must be one of {', '.join(AUDIENCES)}")
    part = str(params.get("part") or "live").strip().lower()
    if part not in PARTS:
        raise ValueError(f"part must be one of {', '.join(PARTS)}")
    focus = params.get("focus")
    if focus not in (None, ""):
        try:
            focus = int(focus)
        except (TypeError, ValueError):
            raise ValueError("focus must be one of our players' shirt numbers") from None
        if not 1 <= focus <= 99:
            raise ValueError("focus must be one of our players' shirt numbers")
    match_id = str(params.get("match_id") or "").strip() or None
    return {"match_id": match_id, "part": part, "audience": audience, "language": language_of(params.get("language")),
            "focus": focus or None}


def tool(name: str, **arguments) -> dict:
    """One of local_tools.py's tools, called directly; its error as ValueError."""
    try:
        return AGENT.TOOL_FUNCTIONS[name](arguments)
    except AGENT.ToolError as exc:
        raise ValueError(str(exc)) from None


def facts(match_id: str | None, part: str, focus: int | None) -> dict:
    """What a recap may say, from the same tools the agents call: the match, the plan, the key plays, the running."""
    summary = tool("get_match_summary", match_id=match_id)
    match = summary["match"]
    period = {"period": 1} if part == "half" else {}
    plays = tool("list_plays", match_id=match["match_id"], key_only=True, **period).get("plays", [])
    running = tool("get_running", match_id=match["match_id"], every_player=bool(focus))
    out = {"match": match, "numbers": summary.get("numbers"), "game_plan": summary.get("game_plan"),
           "deviations": summary.get("deviations"), "flagged_for_the_bench": summary.get("flagged_for_the_bench"),
           "key_plays": [{k: p.get(k) for k in ("minute", "type", "team", "summary")} for p in plays],
           "running": {k: running.get(k) for k in ("distance_km", "high_speed_m", "sprints", "biggest_drops", "answer")
                       if running.get(k) is not None},
           "synthetic_data": summary.get("synthetic_data")}
    if focus:
        player = next((p for p in running.get("players") or [] if p.get("number") == focus), None)
        if player is None:
            raise ValueError(f"#{focus} hasn't played for {match['us']} in this match")
        mention = re.compile(rf"#{focus}\b")
        every = tool("list_plays", match_id=match["match_id"], **period).get("plays", [])
        out["focus"] = {"player": player, "plays": [{k: p.get(k) for k in ("minute", "type", "team", "summary")}
                                                    for p in every if mention.search(p.get("summary") or "")]}
    return out


def task(found: dict, part: str, audience: str, language: str, focus: int | None) -> str:
    """The narrator's task: what to write, in which language, and the facts as JSON."""
    who = ""
    if focus:
        player = found["focus"]["player"]
        who = (f" Follow one player throughout: #{focus}" + (f" {player['name']}" if player.get("name") else "")
               + f" of {found['match']['us']}: his part in the moments, his running, and why it mattered.")
    return (f"Write {AUDIENCES[audience]}, about {PARTS[part]}, in {language}.{who}\n\n"
            f"Facts (JSON, from the match device and the game plan):\n{json.dumps(found, ensure_ascii=False)}")


def narrate_team(prompt: str, client=None) -> str:
    """The narrator agent writes it: on Azure OpenAI, or on the local model (AGENT_BACKEND=local)."""
    from agent_framework import Agent

    narrator = Agent(client=client or client_for_team(), name="narrator", instructions=NARRATOR)
    return asyncio.run(narrator.run(prompt)).text.strip()


def narrate_ollama(prompt: str) -> str:
    model = os.environ.get("OLLAMA_MODEL", "").strip() or AGENT.WARM_MODEL or "gemma4:e2b"
    request = {"model": model, "messages": [{"role": "system", "content": NARRATOR}, {"role": "user", "content": prompt}],
               "options": {"num_predict": 700}, **({"think": AGENT.THINK} if AGENT.THINK is not None else {})}
    for _ in range(2):
        try:
            return "".join((c.get("message") or {}).get("content") or "" for c in AGENT.ollama_chat(request)).strip()
        except AGENT.OllamaError as exc:
            if "think" in request and "does not support thinking" in str(exc):
                request.pop("think")
                continue
            raise
    raise AGENT.OllamaError("no recap")


def recap(params: dict, client=None) -> dict:
    """
    POST /api/recap: a match's story for an audience, in a language, optionally following one of our players.

    {"match_id"?: the match (default: the one being played, or the latest), "part"?: live | half | full,
     "audience"?: fan | coach | analyst | broadcast, "language"?: a code or a name, "focus"?: a shirt number}
    """
    o = options(params)
    found = facts(o["match_id"], o["part"], o["focus"])
    match = found["match"]
    key = (match["match_id"], match.get("status"), match.get("minute"), match.get("score"), o["part"], o["audience"],
           o["language"], o["focus"])
    finished = match.get("status") == "full_time"
    with _cache_lock:
        hit = _cache.get(key)
        if hit and (finished or time.time() - hit[0] < LIVE_TTL):
            _cache.move_to_end(key)
            return {**hit[1], "cached": True}
    prompt = task(found, o["part"], o["audience"], o["language"], o["focus"])
    started = time.monotonic()
    backend = "ollama"
    if client is not None or enabled():
        backend = "local" if BACKEND == "local" and client is None else "azure"
    text = ""
    if backend in ("azure", "local"):
        try:
            text = narrate_team(prompt, client)
        except Exception as exc:  # Azure unreachable, no quota: Ollama's model tells it instead
            AGENT.log(f"the narrator agent can't write ({backend}: {type(exc).__name__}: {exc}): recap on Ollama")
            backend = "ollama"
    if backend == "ollama":
        text = narrate_ollama(prompt)
    if not text:
        raise RuntimeError("the narrator wrote nothing")
    AGENT.log(f"recap {match['match_id']} {o['part']} {o['audience']} {o['language']}"
              f"{' #' + str(o['focus']) if o['focus'] else ''} by {backend} in {time.monotonic() - started:.1f}s")
    result = {"match_id": match["match_id"], "minute": match.get("minute"), "status": match.get("status"),
              "part": o["part"], "audience": o["audience"], "language": o["language"], "focus": o["focus"],
              "recap": text, "backend": backend,
              "model": {"azure": DEPLOYMENT, "local": local_model()}.get(backend),
              "synthetic_data": found.get("synthetic_data"), "cached": False}
    with _cache_lock:
        _cache[key] = (time.time(), result)
        while len(_cache) > CACHE_SIZE:
            _cache.popitem(last=False)
    return result

#!/usr/bin/env python3
"""Generates the match analysis dashboard.

    python3 iot/scripts/build_dashboard.py [--theme BRAND_JSON] [--out FILE]

writes iot/config/grafana/provisioning/dashboards/json/match-analysis.json.
Edit this, not the JSON; Grafana reloads provisioned dashboards on its own
(up to 30 s).

The dashboard follows Grafana's light or dark theme, which p4n4-dashboard
sets to its own (?theme=light|dark). The header takes its accent colours and
name from the p4n4-dashboard theme (../theme/brand.json), and its bar, tint and
mark's name from that theme's themes/<id>/theme.json (iot/scripts/theme.py).

Three scopes:
  - The match (the "match" variable, the one being played by default): both
    teams per 5 minutes (xG, possession, line height, PPDA, width), the plan's
    targets every 5 minutes, the deviations and the plays, the set pieces and
    every player's running. These panels look a year back for that match,
    whatever the time picker says.
  - The season (the time picker, since the season started by default): per
    match, xG for and against, how much of the plan was met, set-piece xG,
    high-speed running, PPDA, and which targets slip most.
  - The club's match device: status, heartbeat, temperatures, tracking.

The panels read what the ingest service copies to InfluxDB (iot/ingest/sinks.py).

Colours: the two sides take two validated categorical steps (us blue, them
aqua), always beside the club codes; the plan's statuses take the status
colours (on plan green, drifting amber, off plan red) with their icons.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
THEME = ROOT.parent / "theme/brand.json"
THEMES = ROOT.parent / "themes"
OUT = ROOT / "config/grafana/provisioning/dashboards/json/match-analysis.json"
DS = {"type": "influxdb", "uid": "influxdb-telemetry"}
EVER = "-400d"
US, THEM = "#3987e5", "#199e70"
STATUS = {"on_plan": ("#0ca30c", "✓ on plan"), "drifting": ("#fab219", "~ drifting"),
          "off_plan": ("#d03b3b", "✕ off plan"), "no_data": ("#6c7d70", "– no data")}
ACCENT = ("#37003c", "#04f5ff")
BAR = ("#04f5ff", "#00ff85 32%", "#e90052 68%", "#963cff")
METRIC_NAMES = {
    "possession_pct": "Possession", "pass_accuracy_pct": "Pass completion", "ppda": "PPDA", "line_height_m": "Line height",
    "width_m": "Width", "length_m": "Team length", "field_tilt_pct": "Field tilt", "counterpress_pct": "Counter-press",
    "high_regains_p90": "High regains /90", "entries_left_pct": "Entries left", "entries_centre_pct": "Entries centre",
    "entries_right_pct": "Entries right", "build_up_short_pct": "Short goal kicks", "shots_conceded_p90": "Shots conceded /90",
    "xg_for_p90": "xG /90", "xg_against_p90": "xG against /90", "formation_in_possession": "Shape in possession",
    "formation_out_of_possession": "Shape out of possession", "set_piece_first_contact_pct": "Set pieces: first contact",
    "set_piece_defence_pct": "Defending set pieces", "set_piece_xg_p90": "Set-piece xG /90",
    "set_piece_xg_against_p90": "Set-piece xG against /90", "hsr_p90": "High-speed running /90", "sprints_p90": "Sprints /90",
    "load_drop_pct": "Running drop",
}
TYPE_NAMES = {"goal": "Goals", "shot": "Shots (xG ≥ 0.08)", "save": "Saves", "set_piece": "Set pieces", "high_regain": "High regains",
              "counter_attack": "Counter-attacks", "build_up": "Build-ups", "press_broken": "Presses played through",
              "foul": "Fouls won", "formation_change": "Formation changes"}

SITE = 'r.site_id == "${site}"'
MATCH = f'{SITE} and r.match_id == "${{match}}"'


def rows(measurement: str, field: str | None, where: str, start: str = EVER) -> str:
    stop = "v.timeRangeStop" if start != EVER else "now()"
    test = f' and r._field == "{field}"' if field else ""
    return (f"from(bucket: v.defaultBucket)\n  |> range(start: {start}, stop: {stop})\n"
            f'  |> filter(fn: (r) => r._measurement == "{measurement}"{test} and {where})\n')


def ds_target(query: str, ref: str = "A") -> dict:
    return {"refId": ref, "datasource": DS, "query": query}


def label_expr(names: dict, column: str) -> str:
    return "".join(f'if r.{column} == "{k}" then "{v}" else ' for k, v in names.items()) + f"r.{column}"


HEADER_CSS = """<style>
.ma-head{--ma-accent:@DARK@;--ma-line:color-mix(in srgb,currentColor 14%,transparent);position:relative;overflow:hidden;
  display:flex;justify-content:space-between;align-items:center;gap:24px;height:100%;padding:5px 20px 0 22px;box-sizing:border-box;
  border:1px solid var(--ma-line);border-radius:6px;background:linear-gradient(100deg,color-mix(in srgb,@TINT@ 55%,transparent),transparent 66%)}
.theme-light .ma-head{--ma-accent:@LIGHT@}
/* The theme's bar along the top */
.ma-head::before{content:"";position:absolute;inset:0 0 auto 0;height:5px;background:linear-gradient(90deg,@BAR@)}
.ma-id{display:flex;align-items:center;gap:18px;min-width:0}
.ma-id img{width:44px;height:44px;object-fit:contain}
.ma-sep{align-self:stretch;width:1px;margin:16px 0;background:var(--ma-line)}
.ma-eyebrow{font-size:12px;font-weight:600;color:var(--ma-accent);letter-spacing:.06em;text-transform:uppercase}
.ma-title{font-size:21px;font-weight:600;line-height:1.3}
.ma-sub{font-size:12.5px;opacity:.7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ma-chips{display:flex;gap:8px;flex-shrink:0}
.ma-chip{display:inline-flex;gap:7px;padding:5px 12px;border:1px solid var(--ma-line);border-radius:999px;font-size:12px;white-space:nowrap}
.ma-chip span{opacity:.75}.ma-chip b{font-weight:500;font-family:ui-monospace,monospace;font-size:11.5px}
.ma-sw{display:inline-block;width:9px;height:9px;border-radius:2px;margin-right:4px}
/* The theme's art (theme.json grafana.art: a file of its site/art/), in the header's empty middle */
.ma-art{position:absolute;left:50%;top:6px;bottom:0;width:210px;background:no-repeat center/contain;pointer-events:none}
@media (max-width:1000px){.ma-sub,.ma-legend,.ma-art{display:none}}
</style>"""


class Builder:
    def __init__(self, theme: dict, spec: dict | None = None) -> None:
        self.theme = theme
        self.spec = spec or {}  # themes/<id>/theme.json
        self.id = 0

    def panel(self, kind: str, title: str, description: str, grid: tuple, targets: list, **extra) -> dict:
        self.id += 1
        x, y, w, h = grid
        return {"id": self.id, "type": kind, "title": title, "description": description, "datasource": DS,
                "gridPos": {"x": x, "y": y, "w": w, "h": h}, "targets": targets, **extra}

    def header(self) -> dict:
        colors = self.theme.get("colors", {})
        ok = lambda c: c if isinstance(c, str) and re.fullmatch(r"#[0-9A-Fa-f]{3,8}", c) else None  # noqa: E731
        light = ok(colors.get("light", {}).get("accent")) or ACCENT[0]
        dark = ok(colors.get("dark", {}).get("accent")) or ACCENT[1]
        name = self.theme.get("appName") if isinstance(self.theme.get("appName"), str) else "Match Intelligence"
        tint = ok(self.spec.get("grafana", {}).get("tint")) or ACCENT[0]
        bar = [c for c in self.spec.get("bar", []) if isinstance(c, str) and re.fullmatch(r"#[0-9A-Fa-f]{3,8}( \d+%)?", c)] or BAR
        mark = self.spec.get("mark") if isinstance(self.spec.get("mark"), str) else name
        art = ""
        if isinstance(self.spec.get("grafana", {}).get("art"), str) and self.spec.get("_dir"):
            svg = Path(self.spec["_dir"]) / "site" / "art" / self.spec["grafana"]["art"]
            art = f'<span class="ma-art" style="background-image:url(data:image/svg+xml;base64,{base64.b64encode(svg.read_bytes()).decode()})"></span>'
        content = HEADER_CSS.replace("@LIGHT@", light).replace("@DARK@", dark).replace("@TINT@", tint).replace("@BAR@", ",".join(bar)) + f"""
<div class="ma-head">{art}
  <div class="ma-id">
    <img src="./public/img/grafana_icon.svg" alt="{html.escape(mark)}">
    <span class="ma-sep"></span>
    <div style="min-width:0">
      <div class="ma-eyebrow">{html.escape(name)}</div>
      <div class="ma-title">${{match}}</div>
      <div class="ma-sub">Measured by the club's match device every 5 minutes, against the staff's game plan</div>
    </div>
  </div>
  <div class="ma-chips">
    <div class="ma-chip ma-legend"><span><i class="ma-sw" style="background:{US}"></i>Brighton <i class="ma-sw" style="background:{THEM};margin-left:8px"></i>Opponent</span></div>
    <div class="ma-chip"><span>Device</span><b>${{node}}</b></div>
  </div>
</div>"""
        self.id += 1
        return {"id": self.id, "type": "text", "title": "", "transparent": True, "gridPos": {"x": 0, "y": 0, "w": 24, "h": 3},
                "options": {"mode": "html", "content": content}}

    def stat(self, title: str, description: str, query: str, grid: tuple, colour: str, unit: str = "none",
             decimals: int = 1, no_value: str = "–") -> dict:
        return self.panel("stat", title, description, grid, [ds_target(query)],
                          fieldConfig={"defaults": {"unit": unit, "decimals": decimals, "noValue": no_value,
                                                    "color": {"mode": "fixed", "fixedColor": colour}}, "overrides": []},
                          options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                                   "colorMode": "value", "graphMode": "none", "textMode": "value", "justifyMode": "auto",
                                   "wideLayout": True, "showPercentChange": False})

    def side_mean(self, field: str, ours: bool) -> str:
        return (rows("team_window", field, MATCH + f' and r.ours == "{str(ours).lower()}"')
                + '  |> group()\n  |> mean()\n  |> keep(columns: ["_value"])')

    def side_sum(self, field: str, ours: bool) -> str:
        return (rows("team_window", field, MATCH + f' and r.ours == "{str(ours).lower()}"')
                + '  |> group()\n  |> sum()\n  |> keep(columns: ["_value"])')

    def by_minute(self, field: str) -> str:
        """Both sides' field per window: columns minute, Brighton, Opponent."""
        return (rows("team_window", None, MATCH + f' and (r._field == "{field}" or r._field == "minute")')
                + '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
                + '  |> map(fn: (r) => ({minute: r.minute, side: if r.ours == "true" then "Brighton" else "Opponent", _value: '
                + f'float(v: r.{field})}}))\n'
                + '  |> group()\n  |> pivot(rowKey: ["minute"], columnKey: ["side"], valueColumn: "_value")\n'
                + '  |> sort(columns: ["minute"])')

    def trend(self, title: str, description: str, field: str, grid: tuple, unit: str = "none") -> dict:
        return self.panel("trend", title, description, grid, [ds_target(self.by_minute(field))],
                          fieldConfig={"defaults": {"unit": unit, "custom": {"lineWidth": 2, "pointSize": 7, "showPoints": "always",
                                                                             "spanNulls": True, "axisBorderShow": False}},
                                       "overrides": [self.colour("Brighton", US), self.colour("Opponent", THEM),
                                                     {"matcher": {"id": "byName", "options": "minute"},
                                                      "properties": [{"id": "unit", "value": "none"}, {"id": "displayName", "value": "Minute"}]}]},
                          options={"xField": "minute", "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                                   "tooltip": {"mode": "multi", "sort": "none"}})

    @staticmethod
    def colour(name: str, colour: str) -> dict:
        return {"matcher": {"id": "byName", "options": name}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": colour}}]}

    def xg_bars(self, grid: tuple) -> dict:
        flux = (rows("team_window", None, MATCH + ' and (r._field == "xg" or r._field == "minute")')
                + '  |> pivot(rowKey: ["_time"], columnKey: ["_field"], valueColumn: "_value")\n'
                + '  |> map(fn: (r) => ({minute: string(v: int(v: r.minute)) + "\'", order: r.minute, side: if r.ours == "true" then "Brighton" else "Opponent", _value: r.xg}))\n'
                + '  |> group()\n  |> pivot(rowKey: ["minute", "order"], columnKey: ["side"], valueColumn: "_value")\n'
                + '  |> sort(columns: ["order"])\n  |> drop(columns: ["order"])')
        return self.panel("barchart", "xG per 5 minutes", "Each side's expected goals in every 5 minutes of the match: who was creating, and when.",
                          grid, [ds_target(flux)],
                          fieldConfig={"defaults": {"min": 0, "decimals": 2, "custom": {"fillOpacity": 90, "lineWidth": 0}},
                                       "overrides": [self.colour("Brighton", US), self.colour("Opponent", THEM)]},
                          options={"xField": "minute", "orientation": "vertical", "barWidth": 0.85, "groupWidth": 0.8, "showValue": "never",
                                   "stacking": "none", "xTickLabelSpacing": 0,
                                   "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                                   "tooltip": {"mode": "multi", "sort": "none"}})

    def plan_grid(self, grid: tuple) -> dict:
        """The plan's targets every 5 minutes: a row per target, a column per window, the cell its status."""
        names = label_expr(METRIC_NAMES, "metric")
        flux = (rows("plan_check", "minute", MATCH)
                + f'  |> map(fn: (r) => ({{target: {names}, minute: string(v: int(v: r._value)) + "\'", order: r._value, status: r.status}}))\n'
                + '  |> group()\n  |> sort(columns: ["order"])\n'
                + '  |> pivot(rowKey: ["target"], columnKey: ["minute"], valueColumn: "status")')
        mappings = [{"type": "value", "options": {k: {"text": label.split(" ")[0], "color": colour, "index": i}
                                                   for i, (k, (colour, label)) in enumerate(STATUS.items())}}]
        return self.panel("table", "The game plan every 5 minutes",
                          "Each target of the plan in each 5-minute window: ✓ on plan, ~ drifting (within the tolerance), ✕ off plan. "
                          "A deviation opens when three windows in a row (15 minutes) are off plan together.",
                          grid, [ds_target(flux)],
                          fieldConfig={"defaults": {"mappings": mappings, "custom": {"align": "center", "cellOptions": {"type": "color-background", "mode": "basic"},
                                                                                       "width": 46}},
                                       "overrides": [{"matcher": {"id": "byName", "options": "target"},
                                                      "properties": [{"id": "displayName", "value": "Target"}, {"id": "custom.width", "value": 190},
                                                                     {"id": "custom.align", "value": "left"},
                                                                     {"id": "custom.cellOptions", "value": {"type": "auto"}}]}]},
                          options={"showHeader": True, "cellHeight": "sm", "footer": {"show": False}})

    def deviations(self, grid: tuple) -> dict:
        names = label_expr(METRIC_NAMES, "metric")
        flux = (rows("plan_deviation", "minute", MATCH)
                + f'  |> map(fn: (r) => ({{_time: r._time, minute: string(v: int(v: r._value)) + "\'", target: {names}, priority: r.priority}}))\n'
                + '  |> group()\n  |> sort(columns: ["_time"])\n  |> drop(columns: ["_time"])')
        return self.panel("table", "Deviations", "Every deviation from the plan in the match, with its minute. Key targets are high priority. "
                          "The analyst's calls are in the analysis room.", grid, [ds_target(flux)],
                          fieldConfig={"defaults": {"custom": {"align": "left"}}, "overrides": [
                              {"matcher": {"id": "byName", "options": "priority"},
                               "properties": [{"id": "mappings", "value": [{"type": "value", "options": {
                                   "high": {"text": "★ key", "color": STATUS["off_plan"][0], "index": 0},
                                   "normal": {"text": "normal", "color": STATUS["drifting"][0], "index": 1}}}]},
                                              {"id": "custom.cellOptions", "value": {"type": "color-text"}}]}]},
                          options={"showHeader": True, "cellHeight": "sm", "footer": {"show": False}})

    def plays(self, grid: tuple) -> dict:
        names = label_expr(TYPE_NAMES, "type")
        flux = (rows("team_play", "count", MATCH)
                + f'  |> map(fn: (r) => ({{type: {names}, side: if r.ours == "true" then "Brighton" else "Opponent", _value: r._value}}))\n'
                + '  |> group(columns: ["type", "side"])\n  |> sum()\n  |> group()\n'
                + '  |> pivot(rowKey: ["type"], columnKey: ["side"], valueColumn: "_value")')
        return self.panel("barchart", "Technical plays", "What the device found, per type and side.", grid, [ds_target(flux)],
                          fieldConfig={"defaults": {"min": 0, "decimals": 0, "custom": {"fillOpacity": 90, "lineWidth": 0}},
                                       "overrides": [self.colour("Brighton", US), self.colour("Opponent", THEM)]},
                          options={"xField": "type", "orientation": "horizontal", "barWidth": 0.8, "groupWidth": 0.75, "showValue": "auto",
                                   "stacking": "none", "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                                   "tooltip": {"mode": "multi", "sort": "none"}})

    def running(self, grid: tuple) -> dict:
        """Our players' running over the match: one row per shirt number."""
        flux = (rows("player_run", None, MATCH + ' and r.ours == "true"')
                + '  |> filter(fn: (r) => r._field != "minute")\n'
                + '  |> group(columns: ["number", "_field"])\n'
                + '  |> reduce(identity: {v: 0.0}, fn: (r, accumulator) => ({v: if r._field == "top_speed_ms" then '
                + '(if float(v: r._value) > accumulator.v then float(v: r._value) else accumulator.v) else accumulator.v + float(v: r._value)}))\n'
                + '  |> group()\n  |> pivot(rowKey: ["number"], columnKey: ["_field"], valueColumn: "v")\n'
                + '  |> map(fn: (r) => ({Player: "#" + r.number, Minutes: r.seconds / 60.0, km: r.distance_m / 1000.0,\n'
                + '      "m a minute": r.distance_m / r.seconds * 60.0, "High-speed m": r.hsr_m, Sprints: r.sprints,\n'
                + '      "Top km/h": r.top_speed_ms * 3.6}))\n  |> sort(columns: ["Minutes"], desc: true)')
        decimals = {"Minutes": 0, "km": 1, "m a minute": 0, "High-speed m": 0, "Sprints": 0, "Top km/h": 1}
        return self.panel("table", "Our players' running", "Every player of ours over the match: distance, ground covered a minute, "
                          "running over 19.8 km/h, sprints over 25.2 km/h, top speed.", grid, [ds_target(flux)],
                          fieldConfig={"defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                                       "overrides": [{"matcher": {"id": "byName", "options": k}, "properties": [{"id": "decimals", "value": v}]}
                                                     for k, v in decimals.items()]},
                          options={"showHeader": True, "cellHeight": "sm", "footer": {"show": False}})

    # ── the season ───────────────────────────────────────────────────────────

    def per_match(self, field: str, agg: str = "sum") -> str:
        """Per match (by its kick-off fixture), Brighton and Opponent: columns match, Brighton, Opponent."""
        return (rows("team_window", field, SITE, "v.timeRangeStart")
                + f'  |> group(columns: ["match_id", "ours"])\n  |> {agg}()\n'
                + '  |> map(fn: (r) => ({match: r.match_id, side: if r.ours == "true" then "Brighton" else "Opponent", _value: float(v: r._value)}))\n'
                + '  |> group()\n  |> pivot(rowKey: ["match"], columnKey: ["side"], valueColumn: "_value")\n  |> sort(columns: ["match"])')

    def season_bars(self, title: str, description: str, field: str, grid: tuple, agg: str = "sum", decimals: int = 2) -> dict:
        return self.panel("barchart", title, description, grid, [ds_target(self.per_match(field, agg))],
                          fieldConfig={"defaults": {"min": 0, "decimals": decimals, "custom": {"fillOpacity": 90, "lineWidth": 0}},
                                       "overrides": [self.colour("Brighton", US), self.colour("Opponent", THEM)]},
                          options={"xField": "match", "orientation": "vertical", "barWidth": 0.85, "groupWidth": 0.75, "showValue": "never",
                                   "stacking": "none", "xTickLabelRotation": -30, "xTickLabelMaxLength": 22,
                                   "legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"},
                                   "tooltip": {"mode": "multi", "sort": "none"}})

    def plan_share(self, grid: tuple) -> dict:
        flux = (rows("plan_check", "on_plan", SITE + ' and r.status != "no_data"', "v.timeRangeStart")
                + '  |> group(columns: ["match_id"])\n  |> mean()\n'
                + '  |> map(fn: (r) => ({match: r.match_id, "On plan": r._value * 100.0}))\n  |> group()\n  |> sort(columns: ["match"])')
        return self.panel("barchart", "On the plan, per match", "Of the targets checked every 5 minutes, the share that were on plan, in each match.",
                          grid, [ds_target(flux)],
                          fieldConfig={"defaults": {"min": 0, "max": 100, "unit": "percent", "decimals": 0,
                                                    "color": {"mode": "fixed", "fixedColor": STATUS["on_plan"][0]},
                                                    "custom": {"fillOpacity": 90, "lineWidth": 0}}, "overrides": []},
                          options={"xField": "match", "orientation": "vertical", "barWidth": 0.7, "showValue": "auto", "stacking": "none",
                                   "xTickLabelRotation": -30, "xTickLabelMaxLength": 22,
                                   "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "single"}})

    def slipping(self, grid: tuple) -> dict:
        names = label_expr(METRIC_NAMES, "metric")
        flux = (rows("plan_deviation", "count", SITE, "v.timeRangeStart")
                + f'  |> map(fn: (r) => ({{target: {names}, _value: r._value}}))\n'
                + '  |> group(columns: ["target"])\n  |> sum()\n  |> group()\n  |> sort(columns: ["_value"], desc: true)\n'
                + '  |> rename(columns: {_value: "Deviations"})')
        return self.panel("barchart", "Which targets slip", "Deviations per target over the period: where the team most often leaves the plan.",
                          grid, [ds_target(flux)],
                          fieldConfig={"defaults": {"min": 0, "decimals": 0, "color": {"mode": "fixed", "fixedColor": STATUS["drifting"][0]},
                                                    "custom": {"fillOpacity": 90, "lineWidth": 0}}, "overrides": []},
                          options={"xField": "target", "orientation": "horizontal", "barWidth": 0.7, "showValue": "auto", "stacking": "none",
                                   "legend": {"showLegend": False, "displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "single"}})

    # ── the device ───────────────────────────────────────────────────────────

    def device(self, y: int) -> list[dict]:
        node = 'r.device == "${node}"'
        status = ('from(bucket: v.defaultBucket)\n  |> range(start: -1d)\n'
                  f'  |> filter(fn: (r) => r._measurement == "team_node" and r._field == "up" and {node})\n'
                  '  |> last()\n  |> map(fn: (r) => ({_value: if uint(v: now()) - uint(v: r._time) > uint(v: 90000000000) then 2 else if r._value == 1 then 0 else 1}))')
        beat = ('from(bucket: v.defaultBucket)\n  |> range(start: -1d)\n'
                f'  |> filter(fn: (r) => r._measurement == "team_node" and r._field == "up" and {node})\n'
                '  |> last()\n  |> map(fn: (r) => ({_value: float(v: uint(v: r._time)) / 1000000.0}))')

        def series(sensors: list[str], since: str = "v.timeRangeStart") -> str:
            test = " or ".join(f'r.sensor == "{s}"' for s in sensors)
            return (f"from(bucket: v.defaultBucket)\n  |> range(start: {since}, stop: v.timeRangeStop)\n"
                    f'  |> filter(fn: (r) => r._measurement == "sensor_data" and {node} and ({test}))\n'
                    '  |> aggregateWindow(every: v.windowPeriod, fn: mean, createEmpty: false)\n'
                    '  |> map(fn: (r) => ({_time: r._time, _value: r._value, _field: r.sensor}))')
        ts = lambda title, desc, q, grid, unit: self.panel(  # noqa: E731
            "timeseries", title, desc, grid, [ds_target(q)],
            fieldConfig={"defaults": {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 0, "showPoints": "never"}}, "overrides": []},
            options={"legend": {"showLegend": True, "displayMode": "list", "placement": "bottom"}, "tooltip": {"mode": "multi"}})
        state = self.panel("stat", "Status", "Online: a heartbeat in the last 90 s, status healthy.", (0, y, 4, 6), [ds_target(status)],
                           fieldConfig={"defaults": {"noValue": "no signal", "mappings": [{"type": "value", "options": {
                               "0": {"text": "online", "color": STATUS["on_plan"][0], "index": 0},
                               "1": {"text": "degraded", "color": STATUS["drifting"][0], "index": 1},
                               "2": {"text": "no signal", "color": STATUS["off_plan"][0], "index": 2}}}]}, "overrides": []},
                           options={"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "colorMode": "value",
                                    "graphMode": "none", "textMode": "value"})
        last = self.stat("Last heartbeat", "When the device last reported, in the last day.", beat, (0, y + 6, 4, 5), "text", unit="dateTimeFromNow")
        return [state, last,
                ts("Temperature", "CPU and GPU.", series(["temperature_cpu_c", "temperature_gpu_c"]), (4, y, 7, 11), "celsius"),
                ts("Tracking", "Frames tracked per second, players tracked and the ball's visibility.",
                   series(["tracking_fps", "players_tracked", "ball_visible_pct"]), (11, y, 7, 11), "none"),
                ts("Latency", "From the picture to the measurement, in milliseconds.", series(["latency_ms"]), (18, y, 6, 11), "ms")]

    def build(self) -> dict:
        panels = [self.header()]
        y = 3
        panels.append({"id": 900, "type": "row", "title": "The match · ${match}", "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []})
        y += 1
        on_share = (rows("plan_check", "on_plan", MATCH + ' and r.status != "no_data"')
                    + '  |> group()\n  |> mean()\n  |> map(fn: (r) => ({_value: r._value * 100.0}))')
        possession = (rows("team_window", "possession_pct", MATCH + ' and r.ours == "true"') + '  |> group()\n  |> mean()\n  |> keep(columns: ["_value"])')
        panels += [
            self.stat("On the plan", "Of the plan's targets checked every 5 minutes, the share on plan.", on_share, (0, y, 4, 4), STATUS["on_plan"][0], "percent", 0),
            self.stat("Possession", "Ours, over the match's windows.", possession, (4, y, 4, 4), US, "percent", 0),
            self.stat("xG for", "Our expected goals.", self.side_sum("xg", True), (8, y, 4, 4), US, decimals=2),
            self.stat("xG against", "Theirs.", self.side_sum("xg", False), (12, y, 4, 4), THEM, decimals=2),
            self.stat("PPDA", "Our press: their passes in their own 60% per defensive action of ours there (window average). Lower is harder.",
                      self.side_mean("ppda", True), (16, y, 4, 4), US),
            self.stat("Line height", "Our back four's average distance from our goal out of possession, in metres.",
                      self.side_mean("line_height_m", True), (20, y, 4, 4), US, "lengthm"),
        ]
        y += 4
        panels += [self.xg_bars((0, y, 12, 8)), self.plan_grid((12, y, 12, 8))]
        y += 8
        panels += [
            self.trend("Defensive line height", "Each side's back four out of possession, every 5 minutes (m from its own goal).", "line_height_m", (0, y, 8, 8), "lengthm"),
            self.trend("PPDA", "Each side's press every 5 minutes. Lower is harder.", "ppda", (8, y, 8, 8)),
            self.trend("Width in possession", "Each side's width every 5 minutes, in metres.", "width_m", (16, y, 8, 8), "lengthm"),
        ]
        y += 8
        panels += [self.deviations((0, y, 12, 8)), self.plays((12, y, 12, 8))]
        y += 8
        panels += [
            self.stat("Set pieces", "Our deliveries into the box: corners, free kicks within 35 m, long throws.",
                      self.side_sum("set_pieces", True), (0, y, 3, 4), US, decimals=0),
            self.stat("Set-piece xG", "The expected goals of our shots in the 8 s after a set piece.",
                      self.side_sum("set_piece_xg", True), (3, y, 3, 4), US, decimals=2),
            self.stat("Their set pieces", "Their deliveries into our box.", self.side_sum("set_pieces", False), (6, y, 3, 4), THEM, decimals=0),
            self.stat("Set-piece xG against", "Theirs, after their set pieces.", self.side_sum("set_piece_xg", False), (9, y, 3, 4), THEM, decimals=2),
            self.trend("High-speed running", "Each side's running over 19.8 km/h, every 5 minutes, in metres.", "hsr_m", (12, y, 12, 9), "lengthm"),
        ]
        panels.append(self.running((0, y + 4, 12, 9)))
        y += 13
        panels.append({"id": 901, "type": "row", "title": "The season (the selected period)", "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []})
        y += 1
        panels += [
            self.season_bars("xG per match", "Ours and theirs, every match in the period.", "xg", (0, y, 12, 9)),
            self.plan_share((12, y, 12, 9)),
        ]
        y += 9
        panels += [
            self.season_bars("Set-piece xG per match", "Ours and theirs, after set pieces into the box.", "set_piece_xg", (0, y, 12, 9)),
            self.season_bars("High-speed running per match", "Each side's running over 19.8 km/h, in metres.", "hsr_m", (12, y, 12, 9), "sum", 0),
        ]
        y += 9
        panels += [
            self.season_bars("PPDA per match", "The average of each match's windows. Lower is a harder press.", "ppda", (0, y, 12, 9), "mean", 1),
            self.slipping((12, y, 12, 9)),
        ]
        y += 9
        panels.append({"id": 902, "type": "row", "title": "The club's match device", "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []})
        y += 1
        panels += self.device(y)
        matches = ('import "influxdata/influxdb/schema"\nfrom(bucket: v.defaultBucket)\n  |> range(start: -400d)\n'
                   '  |> filter(fn: (r) => r._measurement == "team_fixture" and r._field == "status" and r.site_id == "${site}")\n'
                   '  |> last()\n  |> group()\n  |> sort(columns: ["_time"], desc: true)\n  |> keep(columns: ["match_id"])')
        nodes = ('from(bucket: v.defaultBucket)\n  |> range(start: -30d)\n'
                 '  |> filter(fn: (r) => r._measurement == "team_node" and r._field == "up" and r.site_id == "${site}")\n  |> keep(columns: ["device"])\n  |> distinct(column: "device")')
        variable = lambda name, label, query: {  # noqa: E731
            "name": name, "label": label, "type": "query", "datasource": DS, "query": query, "definition": query,
            "refresh": 2, "sort": 0, "includeAll": False, "multi": False, "current": {}}
        return {
            "uid": "match-analysis", "title": "Match analysis", "tags": ["brighton", "p4n4"],
            "description": "Brighton's match analysis: team data, shape, set pieces and running every 5 minutes against the staff's game plan, the season, and the club's match device.",
            "timezone": "browser", "editable": False, "graphTooltip": 1, "refresh": "10s", "schemaVersion": 41,
            "time": {"from": "now-60d", "to": "now"},
            "templating": {"list": [
                {"name": "site", "label": "Club", "type": "custom", "query": "brighton", "current": {"text": "brighton", "value": "brighton"},
                 "options": [{"text": "brighton", "value": "brighton", "selected": True}], "hide": 2},
                variable("match", "Match", matches),
                variable("node", "Device", nodes),
            ]},
            "panels": panels,
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--theme", default=str(THEME))
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()
    theme = json.loads(Path(args.theme).read_text()) if Path(args.theme).is_file() else {}
    spec = THEMES / str(theme.get("id")) / "theme.json"
    spec = {**json.loads(spec.read_text()), "_dir": str(spec.parent)} if spec.is_file() else {}
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(Builder(theme, spec).build(), indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    main()

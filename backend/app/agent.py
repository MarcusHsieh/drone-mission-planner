"""Chat agent: Claude with tool calls on top of the deterministic planner.

The agent never computes geometry or waypoints itself. It can only call the
tools below, which run the same code the UI buttons use, so everything it
does shows up on the map and can be undone by hand.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

import anthropic

from . import config
from .course import DEFAULT_CORRIDOR_M, Course
from .state import MissionState

log = logging.getLogger("agent")

FEATURES = ["fairway", "green", "tees", "corridor"]

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_holes",
        "description": "List holes 1-9 with par, length, and the size of each hole's mapped fairway. Use when you need course facts you don't already have.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "select_area",
        "description": (
            "Add or remove one hole's course feature from the survey selection, as 3 m grid cells. "
            "feature: 'fairway' (mapped fairway shape), 'green', 'tees' (all tee boxes), or "
            "'corridor' (a strip along the hole's center line, tee to green; use only when asked for "
            "the whole hole or a strip). Returns the resulting groups."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hole": {"type": "integer", "minimum": 1, "maximum": 9},
                "feature": {"type": "string", "enum": FEATURES},
                "mode": {"type": "string", "enum": ["add", "remove"], "description": "Default add."},
                "corridor_width_m": {"type": "number", "minimum": 6, "maximum": 120, "description": f"Only for 'corridor'. Default {DEFAULT_CORRIDOR_M:g}."},
            },
            "required": ["hole", "feature"],
            "additionalProperties": False,
        },
    },
    {
        "name": "clear_selection",
        "description": "Remove every selected cell (and every path preview). Use when the user wants to start over or survey something instead of the current selection.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "get_groups",
        "description": "Get the current groups of connected selected cells with their stats (cells, area, size, holes touched, preview path length and time if previewed) and the totals.",
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "focus_map",
        "description": "Pan and zoom the user's map. Give a hole number, or group ids, or nothing to fit the whole selection.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hole": {"type": "integer", "minimum": 1, "maximum": 9},
                "group_ids": {"type": "array", "items": {"type": "integer"}},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "preview_path",
        "description": "Draw straight lawnmower passes over groups and return length and estimated flight time. Omit group_ids to preview every group.",
        "input_schema": {
            "type": "object",
            "properties": {"group_ids": {"type": "array", "items": {"type": "integer"}}},
            "additionalProperties": False,
        },
    },
]


def system_prompt(course: Course) -> str:
    holes = "\n".join(
        f"- Hole {h['hole']}: par {h['par']}, {h['length_m']} m tee to green, fairway {h['fairway_area_m2']} m2"
        for h in course.hole_summary()
    )
    return f"""You are the mission-planning assistant in a drone survey app for {course.name}.
The drone docks and charges at the hole 1 tee box. The map shows a 3 m grid anchored at the dock.
The user selects grid cells to survey; connected cells form numbered groups (Group 1, Group 2, ...).

Course (holes 1-9 only; holes 10-18 are not available):
{holes}

How to work:
- Act through the tools. Never invent coordinates, areas, distances or times; quote the numbers tools return.
- For a request like "survey the fairway on hole 4": select_area for that feature, focus_map on the hole, then preview_path, then reply.
- "Add" means keep the existing selection. A new survey request that doesn't say "add" or "also" replaces it: call clear_selection first.
- One hole's fairway can come out as more than one group when the mapped fairway has a gap (for example before the green). Say so briefly if it happens.
- If a request is ambiguous or not possible (a hole outside 1-9, a feature that isn't mapped), say so instead of guessing.
- Flights are started by the user with the Fly button; you cannot fly the drone.

Reply in plain text, 1 to 3 short sentences, using the numbers from the tools (area in m2, path length in m, time in minutes and seconds)."""


class Agent:
    def __init__(self, course: Course, state: MissionState) -> None:
        self.course = course
        self.state = state
        self.messages: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self._system = system_prompt(course)
        self._client: anthropic.Anthropic | None = None

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            self._client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None)
        return self._client

    def reset(self) -> None:
        with self.lock:
            self.messages = []

    # -- tools ---------------------------------------------------------------
    def _groups_brief(self) -> dict:
        snap = self.state.snapshot(include_cells=False)
        return {
            "total_cells": snap["total_cells"],
            "total_area_m2": snap["total_area_m2"],
            "groups": [
                {k: g[k] for k in ("id", "label", "cell_count", "area_m2", "size_m", "holes", "farthest_from_dock_m", "preview")}
                for g in snap["groups"]
            ],
            "route": snap["route"],
        }

    def run_tool(self, name: str, args: dict, actions: list[dict]) -> dict:
        if name == "list_holes":
            return {"holes": self.course.hole_summary()}
        if name == "select_area":
            hole = int(args["hole"])
            feature = args["feature"]
            if hole not in self.course.holes:
                return {"error": f"Hole {hole} is not on this map. Holes 1-9 only."}
            if feature not in FEATURES:
                return {"error": f"Unknown feature {feature!r}. Use one of {FEATURES}."}
            if self.course.holes[hole].area(feature) is None:
                return {"error": f"Hole {hole} has no mapped {feature}. Try 'corridor'."}
            width = float(args.get("corridor_width_m") or DEFAULT_CORRIDOR_M)
            cells = self.course.feature_cells(hole, feature, width)
            mode = args.get("mode", "add")
            changed = self.state.edit("remove" if mode == "remove" else "add", cells)
            return {"cells_in_feature": len(cells), "cells_changed": changed, **self._groups_brief()}
        if name == "clear_selection":
            n = self.state.clear()
            self.state.clear_previews()
            return {"cleared_cells": n}
        if name == "get_groups":
            return self._groups_brief()
        if name == "focus_map":
            bounds = self._focus_bounds(args)
            if bounds is None:
                return {"error": "Nothing to focus on: no such hole or group, or the selection is empty."}
            actions.append({"type": "focus", "bounds": bounds})
            return {"ok": True}
        if name == "preview_path":
            ids = args.get("group_ids") or None
            done = self.state.preview(ids)
            if not done:
                return {"error": "No matching groups to preview. Select cells first."}
            actions.append({"type": "preview", "group_ids": done})
            return {"previewed": done, **self._groups_brief()}
        return {"error": f"Unknown tool {name}"}

    def _focus_bounds(self, args: dict) -> list[float] | None:
        from shapely.ops import unary_union

        from .planner import cells_polygon

        if args.get("hole"):
            h = self.course.holes.get(int(args["hole"]))
            if h is None:
                return None
            parts = [h.line.buffer(25)] + ([h.fairway] if h.fairway is not None else []) + ([h.green] if h.green is not None else [])
            return self.course.lonlat_bounds(unary_union(parts))
        with self.state.lock:
            ids = args.get("group_ids") or list(self.state.groups)
            cells = set().union(*[self.state.groups[g] for g in ids if g in self.state.groups]) if ids else set()
        if not cells:
            return None
        return self.course.lonlat_bounds(cells_polygon(cells).buffer(15))

    # -- loop ----------------------------------------------------------------
    def chat(self, text: str) -> dict:
        with self.lock:
            start_len = len(self.messages)
            self.messages.append({"role": "user", "content": text})
            actions: list[dict] = []
            trace: list[dict] = []
            usage = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "calls": 0}
            try:
                reply = self._loop(actions, trace, usage)
            except Exception:
                # Keep the history valid for the next turn.
                del self.messages[start_len:]
                raise
            log.info("agent turn: model=%s calls=%d in=%d out=%d cache_read=%d", config.AGENT_MODEL, usage["calls"], usage["input_tokens"], usage["output_tokens"], usage["cache_read_input_tokens"])
            return {"reply": reply, "actions": actions, "tool_calls": trace, "usage": usage, "model": config.AGENT_MODEL}

    def _loop(self, actions: list[dict], trace: list[dict], usage: dict) -> str:
        for _ in range(config.AGENT_MAX_STEPS):
            resp = self.client.messages.create(
                model=config.AGENT_MODEL,
                max_tokens=4096,
                system=self._system,
                tools=TOOLS,
                messages=self.messages,
                output_config={"effort": config.AGENT_EFFORT},
                cache_control={"type": "ephemeral"},
            )
            usage["calls"] += 1
            for k in ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"):
                usage[k] += getattr(resp.usage, k, 0) or 0
            # Append the full content (thinking blocks included) unchanged.
            self.messages.append({"role": "assistant", "content": resp.content})
            if resp.stop_reason == "refusal":
                return "Sorry, I can't help with that request."
            if resp.stop_reason != "tool_use":
                text = " ".join(b.text for b in resp.content if b.type == "text").strip()
                if resp.stop_reason == "max_tokens":
                    text += " (reply cut short)"
                return text or "Done."
            results = []
            for block in resp.content:
                if block.type != "tool_use":
                    continue
                try:
                    out = self.run_tool(block.name, dict(block.input or {}), actions)
                except Exception as e:  # report tool bugs to the model rather than crash the turn
                    log.exception("tool %s failed", block.name)
                    out = {"error": f"{type(e).__name__}: {e}"}
                trace.append({"name": block.name, "input": block.input, "ok": "error" not in out, "error": out.get("error")})
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(out),
                    **({"is_error": True} if "error" in out else {}),
                })
            self.messages.append({"role": "user", "content": results})
        return "I stopped after too many steps. Try a simpler request."

"""Agent loop test with a scripted fake client (no API key or network needed)."""

from types import SimpleNamespace as NS

from app.agent import Agent
from app.course import Course
from app.state import MissionState


def _usage():
    return NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def _tool(id_, name, inp):
    return NS(type="tool_use", id=id_, name=name, input=inp)


class FakeMessages:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kw):
        self.calls.append({**kw, "messages": list(kw["messages"])})
        content, stop = self.script.pop(0)
        return NS(content=content, stop_reason=stop, usage=_usage())


def test_survey_hole_4_flow():
    course = Course()
    state = MissionState(course)
    agent = Agent(course, state)
    fake = FakeMessages([
        ([NS(type="thinking", thinking="", signature="x"),
          _tool("t1", "select_area", {"hole": 4, "feature": "fairway"}),
          _tool("t2", "focus_map", {"hole": 4})], "tool_use"),
        ([_tool("t3", "preview_path", {})], "tool_use"),
        ([NS(type="text", text="Selected hole 4's fairway.")], "end_turn"),
    ])
    agent._client = NS(messages=fake)

    out = agent.chat("survey the fairway on hole 4")

    assert out["reply"] == "Selected hole 4's fairway."
    assert [t["name"] for t in out["tool_calls"]] == ["select_area", "focus_map", "preview_path"]
    assert all(t["ok"] for t in out["tool_calls"])
    kinds = [a["type"] for a in out["actions"]]
    assert kinds == ["focus", "preview"]
    assert len(state.groups) == 2 and len(state.previews) == 2
    # Both tool results went back in one user message, and history alternates.
    second = fake.calls[1]["messages"]
    assert [m["role"] for m in second] == ["user", "assistant", "user"]
    assert len(second[2]["content"]) == 2
    from app import config
    assert fake.calls[0]["model"] == config.AGENT_MODEL


def test_bad_hole_reports_error_and_history_survives_api_failure():
    course = Course()
    agent = Agent(course, MissionState(course))
    fake = FakeMessages([
        ([_tool("t1", "select_area", {"hole": 14, "feature": "fairway"})], "tool_use"),
        ([NS(type="text", text="Hole 14 isn't available.")], "end_turn"),
    ])
    agent._client = NS(messages=fake)
    out = agent.chat("survey hole 14")
    assert out["tool_calls"][0]["ok"] is False
    n = len(agent.messages)

    class Boom:
        def create(self, **kw):
            raise RuntimeError("network down")

    agent._client = NS(messages=Boom())
    try:
        agent.chat("again")
    except RuntimeError:
        pass
    assert len(agent.messages) == n


def test_plan_scan_and_repair_tools():
    from app.scan import DivotField
    from app.sim import DroneSim
    from app.task_queue import TaskQueue

    course = Course()
    state = MissionState(course)
    sim = DroneSim(course.frame)
    q = TaskQueue(course, sim)
    field = DivotField(course, q, sim)
    field.scatter(4, holes=[3], seed=1)
    agent = Agent(course, state, q, field)
    actions = []
    out = agent.run_tool("plan_scan", {"holes": [2, 3]}, actions)
    assert out["holes"] == [2, 3] and out["route_m"] > 0 and isinstance(out["fits_one_battery"], bool)
    assert [p["hole"] for p in out["per_hole"]] == [2, 3]
    assert state.route() is not None and {a["type"] for a in actions} == {"preview", "focus"}
    # Many holes in one scan: too much for one battery, and the tool says so.
    big = agent.run_tool("plan_scan", {"holes": list(range(1, 10))}, [])
    assert big["fits_one_battery"] is False and big["est_mah"] > big["usable_mah_per_battery"]
    # The agent sees what was found, never where the hidden divots are or how many were missed.
    brief = agent.run_tool("get_tasks", {}, [])
    assert brief["scan"] == {"scans_flown": 0, "scanning": False, "divots_found": 0}
    assert "placed" not in str(brief)
    q.add_found(50.0, 50.0, None)
    rep = agent.run_tool("plan_repair", {}, [])
    assert rep["counts"]["pending"] == 1 and len(rep["legs"]) == 1
    assert "error" in agent.run_tool("plan_scan", {"holes": [12]}, [])

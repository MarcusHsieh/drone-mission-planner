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

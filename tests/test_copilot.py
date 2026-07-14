"""Tests for the conversational co-pilot loop. | 对话副驾循环测试。

Uses a stub LLM (no network) to exercise tool-calling and the destructive
confirmation gate. 用桩 LLM（不联网）验证工具调用与破坏性确认门。

Run: PYTHONPATH=src pytest tests/ -q
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from sentinel.engines.copilot import DESTRUCTIVE, new_state, respond, tool_specs  # noqa: E402


# -- stub LLM message/tool-call objects | 桩消息/工具调用对象 -----------------

class _Fn:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    def __init__(self, tid, name, arguments):
        self.id = tid
        self.function = _Fn(name, arguments)


class _Msg:
    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class _StubLLM:
    """EN: Returns a scripted message on each .chat() call. | ZH: 每次 chat 返回脚本消息。"""

    def __init__(self, script):
        self._script = list(script)
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        return self._script.pop(0)


def test_tool_specs_match_registry():
    names = {s["function"]["name"] for s in tool_specs()}
    assert {"discover", "alerts", "deploy_alerts", "apply"} <= names
    # each spec has a JSON-schema parameters object
    assert all("parameters" in s["function"] for s in tool_specs())


def test_readonly_tool_flow_executes_and_replies():
    # LLM: first asks to call discover, then gives a final answer.
    script = [
        _Msg(tool_calls=[_ToolCall("c1", "discover",
                                   '{"repo_path": "/no/such/repo"}')]),
        _Msg(content="已扫描完成。"),
    ]
    llm = _StubLLM(script)
    reply, state = respond("scan my repo", new_state(), llm)
    assert reply == "已扫描完成。"
    assert state["pending"] is None
    # discover ran (a tool result message exists)
    assert any(m.get("role") == "tool" for m in state["messages"])


def test_destructive_tool_pauses_for_confirmation():
    script = [_Msg(tool_calls=[_ToolCall("d1", "apply",
                                         '{"repo_path": "/x", "branch": "b"}')])]
    llm = _StubLLM(script)
    reply, state = respond("instrument and commit", new_state(), llm)
    # loop paused BEFORE executing the destructive tool
    assert "确认" in reply
    assert state["pending"] is not None
    assert state["pending"]["name"] == "apply"
    # apply was NOT executed yet -> no tool result for d1
    assert not any(m.get("role") == "tool" and m.get("tool_call_id") == "d1"
                   for m in state["messages"])


def test_confirmation_then_executes():
    # turn 1: propose destructive -> pending
    llm1 = _StubLLM([_Msg(tool_calls=[_ToolCall("d1", "apply",
                                                '{"repo_path": "/x", "branch": "b"}')])])
    _, state = respond("commit it", new_state(), llm1)
    assert state["pending"] is not None
    # turn 2: user confirms -> apply executes, model wraps up
    llm2 = _StubLLM([_Msg(content="已提交到分支。")])
    reply, state = respond("确认", state, llm2)
    assert state["pending"] is None
    # a tool result for the destructive call now exists
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "d1"
               for m in state["messages"])


def test_cancel_declines_destructive():
    llm1 = _StubLLM([_Msg(tool_calls=[_ToolCall("d1", "deploy_alerts",
                                                '{"repo_path": "/x", "contact_point": "c"}')])])
    _, state = respond("deploy", new_state(), llm1)
    assert state["pending"]["name"] == "deploy_alerts"
    # user declines -> pending cleared, treated as a normal message
    llm2 = _StubLLM([_Msg(content="好的，已取消。")])
    reply, state = respond("先别", state, llm2)
    assert state["pending"] is None
    assert reply == "好的，已取消。"

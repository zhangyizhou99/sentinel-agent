"""Conversational co-pilot. | 对话副驾。

EN: The piece that makes Sentinel feel like an AGENT, not just an app: an LLM that
    plans and drives Sentinel's own tools (the same 9 exposed over MCP) via
    function-calling, in a ReAct-style loop. Destructive tools (apply, deploy_alerts)
    are NEVER auto-executed — the loop pauses and asks the human to confirm first
    (human-in-the-loop). One tool definition, reused three ways: CLI, MCP, co-pilot.
ZH: 让 Sentinel 从“应用”变“agent”的一块：一个 LLM 通过 function-calling 规划并驱动
    Sentinel 自己的工具（与 MCP 暴露的同一批 9 个），ReAct 式循环。破坏性工具（apply、
    deploy_alerts）绝不自动执行 —— 循环会暂停、先请人确认（human-in-the-loop）。
    一份工具定义，三处复用：CLI、MCP、对话副驾。
"""
from __future__ import annotations

import json
from typing import Callable, Dict, List, Optional, Tuple

# EN: reuse the exact tool registry the MCP server exposes. | ZH: 复用 MCP 暴露的同一份工具注册表。
from sentinel.mcp_server import _TOOLS

# EN: tools that change code or external systems — require confirmation.
# ZH: 会改代码或外部系统的工具 —— 需确认。
DESTRUCTIVE = {"apply", "deploy_alerts"}

_AFFIRM = {"yes", "y", "confirm", "ok", "okay", "go", "do it",
           "是", "确认", "好", "好的", "可以", "执行", "同意", "行"}

SYSTEM_PROMPT = (
    "You are Sentinel's co-pilot: an observability engineer that sets up "
    "monitoring for a code repository by calling the provided tools. "
    "Typical flow: discover metrics, design alerts, then export or deploy. "
    "Always ask for the repository path if the user hasn't given one. "
    "Explain briefly what you did after each step. Reply in the user's language. "
    "Destructive tools (apply, deploy_alerts) are gated: the system will ask the "
    "user to confirm before they run, so propose them normally."
)


def tool_specs() -> List[dict]:
    """EN: OpenAI-style function specs from the shared tool registry.
    ZH: 从共享工具注册表生成 OpenAI 风格的函数规格。"""
    return [
        {"type": "function", "function": {
            "name": name, "description": t["description"],
            "parameters": t["inputSchema"]}}
        for name, t in _TOOLS.items()
    ]


def _execute(name: str, args: dict) -> dict:
    entry = _TOOLS.get(name)
    if not entry:
        return {"error": f"unknown tool: {name}"}
    try:
        return entry["func"](**args)
    except Exception as e:  # EN: surface as tool error | ZH: 作为工具错误返回
        return {"error": f"{type(e).__name__}: {e}"}


def _tc_to_dict(tc) -> dict:
    return {"id": tc.id, "type": "function",
            "function": {"name": tc.function.name, "arguments": tc.function.arguments}}


def new_state() -> dict:
    """EN: A fresh conversation state. | ZH: 一个全新的对话状态。"""
    return {"messages": [{"role": "system", "content": SYSTEM_PROMPT}], "pending": None}


def respond(user_text: str, state: dict, llm, max_steps: int = 6) -> Tuple[str, dict]:
    """EN: Advance the conversation by one user turn. Runs the tool-calling loop;
        pauses for confirmation before any destructive tool. `llm` must expose
        `.chat(messages, tools)` returning an assistant message.
    ZH: 推进一轮对话。跑工具调用循环；任何破坏性工具前暂停等确认。`llm` 需有
        `.chat(messages, tools)` 返回 assistant 消息。"""
    messages: List[dict] = state.get("messages") or new_state()["messages"]
    pending = state.get("pending")

    # EN: resolve a pending destructive call from the previous turn. | ZH: 处理上一轮挂起的破坏性调用。
    if pending:
        if user_text.strip().lower() in _AFFIRM:
            result = _execute(pending["name"], pending["args"])
            messages.append({"role": "tool", "tool_call_id": pending["tool_call_id"],
                             "content": json.dumps(result, ensure_ascii=False)})
            state["pending"] = None
            # fall through into the loop to let the model react to the result
        else:
            messages.append({"role": "tool", "tool_call_id": pending["tool_call_id"],
                             "content": json.dumps({"cancelled": True, "reason": user_text},
                                                   ensure_ascii=False)})
            state["pending"] = None
            messages.append({"role": "user", "content": user_text})
    else:
        messages.append({"role": "user", "content": user_text})

    specs = tool_specs()
    for _ in range(max_steps):
        msg = llm.chat(messages, tools=specs)
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            text = msg.content or ""
            messages.append({"role": "assistant", "content": text})
            state["messages"] = messages
            return text, state

        messages.append({"role": "assistant", "content": msg.content or "",
                         "tool_calls": [_tc_to_dict(tc) for tc in tool_calls]})
        confirm: Optional[str] = None
        for tc in tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            if name in DESTRUCTIVE and confirm is None:
                state["pending"] = {"name": name, "args": args, "tool_call_id": tc.id}
                confirm = (
                    f"⚠️ 我准备执行**破坏性操作** `{name}`\n\n"
                    f"参数：`{json.dumps(args, ensure_ascii=False)}`\n\n"
                    f"确认执行吗？回复 **是 / 确认** 执行，其它则取消。"
                )
            elif name in DESTRUCTIVE:
                # EN: another destructive in the same batch -> defer (avoid deadlock).
                # ZH: 同批的另一个破坏性调用 -> 延后（避免死锁）。
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps({"deferred": True}, ensure_ascii=False)})
            else:
                result = _execute(name, args)
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": json.dumps(result, ensure_ascii=False)})
        if confirm:
            state["messages"] = messages
            return confirm, state

    state["messages"] = messages
    return "（达到最大步数，请继续指示 | reached step limit, please continue）", state

"""LLM prompts (bilingual). | LLM 提示词（双语）。

EN: Prompts are provided in both English and Chinese. The engine picks one by
    the `lang` argument ("en" | "zh"). Keep the two versions semantically
    identical so behavior does not drift between languages.
ZH: 提示词提供中英两版，引擎按 `lang` 参数（"en" | "zh"）选择。请保持两版语义
    完全一致，避免语言间行为漂移。
"""
from __future__ import annotations

# EN: System prompt that turns the LLM into a semantic reviewer of a static
#     metrics catalog. It must NOT invent metrics unsupported by the code.
# ZH: 让 LLM 成为静态指标清单的语义评审者的系统提示。它不得臆造代码不支持的指标。

DISCOVERY_SYSTEM_EN = """\
You are Sentinel's discovery reviewer. You are given (1) source code excerpts \
and (2) a draft metrics catalog produced by a static AST scanner. Your job is \
to add ONLY business-critical metrics the static scanner cannot infer \
semantically (e.g. "this function is the core checkout path, monitor its \
success rate"). Rules:
- Never invent metrics unsupported by the code.
- Prefer RED/USE/Golden-Signals categories.
- Output strict JSON: a list of objects with keys id, kind, description, \
signal, category, reason.
"""

DISCOVERY_SYSTEM_ZH = """\
你是 Sentinel 的发现评审者。你会拿到 (1) 源码片段 和 (2) 静态 AST 扫描器产出的 \
指标清单草稿。你的任务是仅补充静态扫描器无法从语义上推断的、对业务关键的指标\
（例如“这个函数是核心下单链路，应监控其成功率”）。规则：
- 绝不臆造代码不支持的指标。
- 优先使用 RED/USE/黄金信号 分类。
- 输出严格 JSON：一个对象列表，键为 id、kind、description、signal、category、reason。
"""

DISCOVERY_USER_EN = """\
## Source excerpts
{code}

## Draft catalog (static)
{catalog}

Return only the additional metrics as JSON.
"""

DISCOVERY_USER_ZH = """\
## 源码片段
{code}

## 草稿清单（静态）
{catalog}

只返回需要补充的额外指标（JSON）。
"""


def discovery_prompt(lang: str, code: str, catalog: str) -> tuple[str, str]:
    """EN: Return (system, user) prompt for the given language.
    ZH: 返回给定语言的 (system, user) 提示对。"""
    if lang == "zh":
        return DISCOVERY_SYSTEM_ZH, DISCOVERY_USER_ZH.format(code=code, catalog=catalog)
    return DISCOVERY_SYSTEM_EN, DISCOVERY_USER_EN.format(code=code, catalog=catalog)

"""Source rewriter. | 源码改写器。

EN: The hard, careful part of L2: actually modify a source file to wire in
    instrumentation, guided by the AST so we insert at correct locations. It only
    handles patterns it is confident about (FastAPI `app = FastAPI()` + middleware)
    and returns None otherwise — never a broken edit.
ZH: L2 里最难、最需小心的部分：在 AST 指引下真正修改源文件、接入埋点，确保插在
    正确位置。只处理有把握的模式（FastAPI 的 `app = FastAPI()` + 中间件），否则
    返回 None——绝不产出坏掉的改动。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass


@dataclass
class SourceEdit:
    """EN: A confident, ready-to-write modification of one file.
    ZH: 一份有把握、可直接落盘的单文件改动。"""

    new_source: str            # EN: full modified file text | ZH: 改后完整文件内容
    app_var: str               # EN: the FastAPI app variable found | ZH: 找到的 FastAPI app 变量
    import_line: int           # EN: where the import was inserted | ZH: import 插入位置
    wire_line: int             # EN: where wiring was inserted | ZH: 接线插入位置


def wire_fastapi(source: str, helper_module: str) -> SourceEdit | None:
    """EN: Inject `from <helper> import sentinel_middleware` and
        `<app>.middleware("http")(sentinel_middleware)` into a FastAPI source.
        Returns None if the file is not a recognizable FastAPI app.
    ZH: 往 FastAPI 源码里注入 import 与中间件接线；若不是可识别的 FastAPI 应用则
        返回 None。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    app_var, app_end_line = _find_fastapi_app(tree)
    if app_var is None:
        return None

    last_import_end = _last_top_level_import_end(tree)

    import_stmt = f"from {helper_module} import sentinel_middleware"
    wire_stmt = f'{app_var}.middleware("http")(sentinel_middleware)'

    lines = source.splitlines()

    # EN: idempotency — do nothing if already wired. | ZH: 幂等 —— 已接线则不动。
    if any(wire_stmt in ln for ln in lines):
        return None

    # EN: insert from the BOTTOM up so earlier line numbers stay valid.
    # ZH: 从下往上插，保证前面的行号不失效。
    inserts = sorted(
        [(app_end_line, wire_stmt), (last_import_end, import_stmt)],
        key=lambda x: x[0],
        reverse=True,
    )
    for line_no, text in inserts:
        lines.insert(line_no, text)

    new_source = "\n".join(lines) + ("\n" if source.endswith("\n") else "")

    # EN: safety net — the result MUST still parse. | ZH: 安全网 —— 结果必须仍能解析。
    try:
        ast.parse(new_source)
    except SyntaxError:
        return None

    return SourceEdit(
        new_source=new_source,
        app_var=app_var,
        import_line=last_import_end + 1,
        wire_line=app_end_line + 1,
    )


# -- AST helpers | AST 辅助 -------------------------------------------------

def _find_fastapi_app(tree: ast.Module) -> tuple[str | None, int]:
    """EN: Find `X = FastAPI(...)` at top level; return (X, end_line).
    ZH: 在顶层找 `X = FastAPI(...)`；返回 (X, 结束行)。"""
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            if _callee_name(node.value.func) == "FastAPI" and node.targets:
                target = node.targets[0]
                if isinstance(target, ast.Name):
                    return target.id, node.end_lineno or node.lineno
    return None, 0


def _last_top_level_import_end(tree: ast.Module) -> int:
    """EN: Line after the last top-level import (0 if none). | ZH: 最后一个顶层 import 之后的行（无则 0）。"""
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = node.end_lineno or node.lineno
    return last


def _callee_name(func: ast.AST) -> str | None:
    """EN: Get the called name: `FastAPI` or `x.FastAPI`. | ZH: 取被调用名：`FastAPI` 或 `x.FastAPI`。"""
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None

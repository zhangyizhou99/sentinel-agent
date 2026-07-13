"""Code unit extraction. | 代码单元抽取。

EN: A "code unit" = one function/method turned into searchable TEXT for the
    TF-IDF index. The text captures semantic clues — the name, arguments,
    docstring, decorators, and what the function calls — so retrieval can tell an
    API handler / DB writer / business path apart from a trivial helper.
ZH: 一个“代码单元” = 把一个函数/方法变成可检索的**文本**喂给 TF-IDF。文本收集
    语义线索——函数名、参数、docstring、装饰器、以及它调用了什么——这样检索才能
    把 API 处理器/写库函数/业务链路 和 无关紧要的小工具区分开。
"""
from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from sentinel.adapters.scanners.python_scanner import _DEFAULT_EXCLUDE_DIRS


@dataclass
class CodeUnit:
    unit_id: str        # EN: stable id "file::symbol" | ZH: 稳定 id "文件::符号"
    file: str
    symbol: str
    line: int
    text: str           # EN: searchable text fed to TF-IDF | ZH: 喂给 TF-IDF 的可检索文本


def extract_code_units(root: str | Path) -> list[CodeUnit]:
    """EN: Walk a repo and turn every function into a CodeUnit.
    ZH: 遍历仓库，把每个函数变成一个 CodeUnit。"""
    root = Path(root)
    files = [root] if root.is_file() else root.rglob("*.py")
    units: list[CodeUnit] = []
    for path in sorted(files):
        if set(path.parts) & _DEFAULT_EXCLUDE_DIRS:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, OSError, UnicodeDecodeError):
            continue
        rel = path.relative_to(root).as_posix() if root.is_dir() else path.name
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                units.append(_unit_from_func(node, rel))
    return units


def _unit_from_func(func: ast.FunctionDef | ast.AsyncFunctionDef, rel: str) -> CodeUnit:
    words: list[str] = [func.name]

    # EN: argument names carry intent (user_id, order, amount...).
    # ZH: 参数名带意图（user_id、order、amount…）。
    words += [a.arg for a in func.args.args]

    # EN: the docstring is a strong semantic signal.
    # ZH: docstring 是很强的语义信号。
    doc = ast.get_docstring(func)
    if doc:
        words.append(doc)

    # EN: decorators reveal role, e.g. @app.post -> "app post".
    # ZH: 装饰器暴露角色，如 @app.post -> "app post"。
    for dec in func.decorator_list:
        words += _names_of(dec)

    # EN: what it CALLS reveals side effects (db.query, requests.get, cache.set).
    # ZH: 它“调用了什么”暴露副作用（db.query、requests.get、cache.set）。
    for n in ast.walk(func):
        if isinstance(n, ast.Call):
            words += _names_of(n.func)

    return CodeUnit(
        unit_id=f"{rel}::{func.name}",
        file=rel,
        symbol=func.name,
        line=func.lineno,
        text=" ".join(words),
    )


def _names_of(node: ast.AST) -> list[str]:
    """EN: Flatten a name/attribute/call chain into words: `a.b.c()` -> [a,b,c].
    ZH: 把 名称/属性/调用 链拍平成词：`a.b.c()` -> [a,b,c]。"""
    out: list[str] = []
    cur = node.func if isinstance(node, ast.Call) else node
    while isinstance(cur, ast.Attribute):
        out.append(cur.attr)
        cur = cur.value
    if isinstance(cur, ast.Name):
        out.append(cur.id)
    return out

"""Apply engine (L2). | 应用引擎（L2）。

EN: Turns the catalog's `missing` metrics into REAL source edits on a new,
    user-named git branch — and LEAVES them as uncommitted working changes so you
    can see, review and edit them in your editor before committing. Your original
    branch is never touched: if you dislike the result, `git checkout <base>` and
    delete the branch — nothing is lost.
ZH: 把清单里的 `missing` 指标变成对源码的**真实修改**，落在一个用户命名的新 git
    分支上，并**保持未提交**——让你能在编辑器里看、审、改，再自己决定是否提交。
    你的原分支不受影响：不满意就 `git checkout <base>` 再删分支，毫无损失。
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sentinel.engines.editor import wire_fastapi
from sentinel.engines.instrument import InstrumentEngine
from sentinel.model.metric import MetricsCatalog, Status


@dataclass
class ApplyResult:
    branch: str
    base_branch: str
    files_changed: list[str] = field(default_factory=list)
    helper_files: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    diff: str = ""
    message: str = ""


class ApplyError(RuntimeError):
    """EN: Raised on any precondition failure. | ZH: 前置条件失败时抛出。"""


class Applier:
    """EN: Commit instrumentation to a new git branch. | ZH: 把埋点提交到新建 git 分支。"""

    def __init__(self, instrument: InstrumentEngine | None = None):
        self.instrument = instrument or InstrumentEngine()

    def apply(self, repo: str | Path, catalog: MetricsCatalog, branch: str) -> ApplyResult:
        repo = Path(repo).resolve()
        branch = (branch or "").strip()
        if not branch:
            raise ApplyError("branch name required | 需要分支名（请自己输入）")

        self._require_git_repo(repo)
        self._require_clean(repo)
        base = self._current_branch(repo)
        self._require_new_branch(repo, branch)

        # EN: build helper content per source file from the instrument engine.
        # ZH: 用 instrument 引擎为每个源文件生成助手内容。
        helpers = {p.target_file: p.content for p in self.instrument.generate(catalog)}
        if not helpers:
            raise ApplyError("no missing metrics to apply | 无缺失指标可补")

        result = ApplyResult(branch=branch, base_branch=base)
        self._git(repo, ["checkout", "-b", branch])
        self._write_edits(repo, helpers, result)

        # EN: intent-to-add new files so they show up in `git diff` too, but do
        #     NOT commit — leave everything as working changes for you to review.
        # ZH: 用 intent-to-add 让新文件也出现在 `git diff`，但**不提交**——把所有改动
        #     留成工作区改动，交给你审阅。
        self._git(repo, ["add", "-N", "."])
        result.diff = self._git(repo, ["diff"]).stdout

        # EN: we stay ON the new branch with uncommitted changes on purpose.
        # ZH: 有意停在新分支上，改动保持未提交。
        result.message = (
            f"switched to new branch '{branch}' with {len(result.files_changed)} file(s) of "
            f"UNCOMMITTED changes — review/edit in your editor, then commit or discard. "
            f"(base was '{base}') | 已切换到新分支 '{branch}'，{len(result.files_changed)} 处改动"
            f"**未提交**，去编辑器里看/改，再自行提交或丢弃（原分支 '{base}'）。"
        )
        return result

    # -- edits | 改动 ------------------------------------------------------

    def _write_edits(self, repo: Path, helpers: dict[str, str], result: ApplyResult) -> None:
        for rel_file, helper_content in sorted(helpers.items()):
            src_path = repo / rel_file
            if not src_path.exists():
                result.skipped.append(rel_file)
                continue

            stem = Path(rel_file).stem
            helper_module = f"{stem}_sentinel"
            helper_path = src_path.parent / f"{helper_module}.py"

            source = src_path.read_text(encoding="utf-8")
            edit = wire_fastapi(source, helper_module)
            if edit is None:
                # EN: can't safely auto-wire — skip source, keep it honest.
                # ZH: 无法安全接线 —— 跳过源码改动，保持诚实。
                result.skipped.append(rel_file)
                continue

            helper_path.write_text(helper_content, encoding="utf-8")
            src_path.write_text(edit.new_source, encoding="utf-8")
            result.helper_files.append(str(helper_path.relative_to(repo)))
            result.files_changed.append(rel_file)

    # -- git preconditions | git 前置检查 ----------------------------------

    def _require_git_repo(self, repo: Path) -> None:
        r = self._git(repo, ["rev-parse", "--is-inside-work-tree"], check=False)
        if r.returncode != 0 or r.stdout.strip() != "true":
            raise ApplyError(f"not a git repo | 不是 git 仓库: {repo}")

    def _require_clean(self, repo: Path) -> None:
        if self._git(repo, ["status", "--porcelain"]).stdout.strip():
            raise ApplyError(
                "working tree not clean — commit or stash first | 工作区不干净，请先提交或 stash"
            )

    def _require_new_branch(self, repo: Path, branch: str) -> None:
        r = self._git(repo, ["rev-parse", "--verify", branch], check=False)
        if r.returncode == 0:
            raise ApplyError(f"branch already exists | 分支已存在: {branch}（请换个名字）")

    def _current_branch(self, repo: Path) -> str:
        return self._git(repo, ["rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    def _git(self, repo: Path, args: list[str], check: bool = True) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["git", "-C", str(repo), *args],
                check=check, capture_output=True, text=True, timeout=120,
            )
        except FileNotFoundError as exc:
            raise ApplyError("git not found | 未找到 git") from exc
        except subprocess.CalledProcessError as exc:
            raise ApplyError(f"git failed | git 失败: {exc.stderr.strip()}") from exc

"""Sentinel TUI. | Sentinel 终端交互界面。

EN: A Textual app: pick a repo path, an LLM provider and a privacy tier from
    dropdowns, click Discover to see the metrics catalog in an interactive table,
    or Instrument to generate埋点. It renders the SAME data the CLI does — only
    the "shell" differs. Run: `python -m sentinel.tui`.
ZH: 一个 Textual 应用：用下拉框选仓库路径、LLM Provider 和隐私档，点 Discover
    在交互表格里看指标清单，或点 Instrument 生成埋点。它渲染的是和 CLI 完全相同
    的数据——只是“外壳”不同。运行：`python -m sentinel.tui`。
"""
from __future__ import annotations

from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.widgets import Button, DataTable, Footer, Header, Input, Select, Static

from sentinel.adapters.scanners.cache import ScanCache
from sentinel.adapters.scanners.python_scanner import PythonScanner
from sentinel.engines.discovery import DiscoveryEngine
from sentinel.engines.instrument import InstrumentEngine
from sentinel.llm.client import PROVIDERS, LLMClient, LLMConfig, PrivacyMode
from sentinel.paths import scan_cache_path
from sentinel.model.metric import Status

# EN: status -> color, same palette as the CLI. | ZH: 状态 -> 颜色，与 CLI 一致。
_STATUS_COLOR = {Status.missing: "yellow", Status.present: "green", Status.partial: "cyan"}


class SentinelApp(App):
    """EN: The interactive discovery/instrument app. | ZH: 交互式发现/埋点应用。"""

    CSS = """
    #controls { height: auto; padding: 1; }
    #controls Input { width: 40; }
    #controls Select { width: 26; }
    Button { margin: 0 1; }
    #summary { height: auto; padding: 1; color: $text-muted; }
    DataTable { height: 1fr; }
    """

    BINDINGS = [("q", "quit", "Quit | 退出")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        # EN: control bar — the "settings panel" you asked for.
        # ZH: 控制栏 —— 你要的“设置面板”。
        with Vertical():
            with Horizontal(id="controls"):
                yield Input(value="../sentinel-sample-app", placeholder="repo path | 仓库路径", id="repo")
                yield Select(
                    [(p, p) for p in sorted(PROVIDERS)],
                    value="modelscope", id="provider", prompt="Provider | 接口",
                )
                yield Select(
                    [(m.value, m.value) for m in PrivacyMode],
                    value=PrivacyMode.air_gapped.value, id="privacy", prompt="Privacy | 隐私档",
                )
                yield Button("Discover | 发现", id="discover", variant="primary")
                yield Button("Instrument | 补埋点", id="instrument", variant="success")
            yield Static("", id="summary")
            yield DataTable(id="table")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.add_columns("Status | 状态", "Metric ID | 指标", "Signal | 信号",
                          "Category | 分类", "Source | 来源", "Sampling | 采样")

    # -- actions | 动作 ----------------------------------------------------

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "discover":
            self._discover()
        elif event.button.id == "instrument":
            self._instrument()

    def _engine(self) -> tuple[DiscoveryEngine, LLMClient]:
        provider = self.query_one("#provider", Select).value
        privacy = self.query_one("#privacy", Select).value
        repo = self.query_one("#repo", Input).value
        llm = LLMClient(LLMConfig(provider=str(provider), privacy_mode=PrivacyMode(str(privacy))))
        cache = ScanCache(scan_cache_path(repo))
        engine = DiscoveryEngine(scanners=[PythonScanner(cache=cache)], llm=llm)
        return engine, llm

    def _discover(self) -> None:
        repo = self.query_one("#repo", Input).value
        summary = self.query_one("#summary", Static)
        table = self.query_one("#table", DataTable)
        table.clear()

        if not Path(repo).exists():
            summary.update(f"[red]repo not found | 仓库不存在: {repo}[/red]")
            return

        engine, llm = self._engine()
        catalog = engine.run(repo)
        for m in catalog.metrics:
            color = _STATUS_COLOR.get(m.status, "white")
            loc = f"{m.source.file}:{m.source.symbol}"
            if m.source.line:
                loc += f":{m.source.line}"
            table.add_row(
                Text(m.status.value, style=color),
                m.id,
                m.signal.value if m.signal else "-",
                m.category or "-",
                loc,
                m.sampling.strategy.value if m.sampling.required else "-",
            )

        s = catalog.summary()
        llm_state = "ON" if llm.available else f"OFF ({llm.why_unavailable()})"
        summary.update(
            f"repo: {catalog.repo}  |  total {s.get('total', 0)}  "
            f"missing {s.get('missing', 0)}  present {s.get('present', 0)}  |  LLM: {llm_state}"
        )

    def _instrument(self) -> None:
        repo = self.query_one("#repo", Input).value
        summary = self.query_one("#summary", Static)
        if not Path(repo).exists():
            summary.update(f"[red]repo not found | 仓库不存在: {repo}[/red]")
            return
        engine, _ = self._engine()
        patches = InstrumentEngine().generate(engine.run(repo))
        for p in patches:
            Path(p.output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(p.output_path).write_text(p.content, encoding="utf-8")
        summary.update(
            f"[green]instrumented | 已补埋点: {len(patches)} file(s) -> .sentinel/instrumentation/[/green]"
        )


def main() -> None:
    SentinelApp().run()


if __name__ == "__main__":
    main()

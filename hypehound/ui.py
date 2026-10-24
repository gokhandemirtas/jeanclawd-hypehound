import logging
from datetime import datetime, timedelta
from rich.align import Align
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widget import Widget
from textual.widgets import Footer, Header, RichLog
from textual.containers import Horizontal, Vertical
from textual.worker import Worker, WorkerState

from .log import attach_textual_handler, log
from .pipeline import DIGEST_NODES, INGEST_NODES, PipelineDeps
from .store import get_stats


# --- Log handler -------------------------------------------------------------

class RichLogHandler(logging.Handler):
    def __init__(self, widget: RichLog) -> None:
        super().__init__()
        self.widget = widget

    def emit(self, record: logging.LogRecord) -> None:
        level = record.levelname.lower()
        colour = {
            "debug": "dim",
            "info": "white",
            "warning": "yellow",
            "error": "red",
            "critical": "bold red",
        }.get(level, "white")
        self.widget.write(f"[{colour}]{self.format(record)}[/{colour}]")


# --- Tube map widget ---------------------------------------------------------

_SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

_STATUS_ICON: dict[str, tuple[str, str]] = {
    "idle":  ("○", "dim white"),
    "done":  ("●", "green"),
    "error": ("✗", "bold red"),
}


class PipelineGraphWidget(Widget):
    DEFAULT_CSS = """
    PipelineGraphWidget {
        width: 1fr;
        height: 100%;
        padding: 1 2;
        content-align: center top;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._pipeline_nodes: list[tuple[str, str]] = []
        self._title: str = "Pipeline"
        self._stats: dict = {}
        self._frame: int = 0
        self._spinner_timer = None

    def _start_spinner(self) -> None:
        if self._spinner_timer is None:
            self._spinner_timer = self.set_interval(1 / 12, self._tick)

    def _stop_spinner(self) -> None:
        if self._spinner_timer is not None:
            self._spinner_timer.stop()
            self._spinner_timer = None
            self._frame = 0

    def _tick(self) -> None:
        self._frame = (self._frame + 1) % len(_SPINNER_FRAMES)
        self.refresh()

    def set_stats(self, stats: dict) -> None:
        self._stats = stats
        self.refresh()

    def load(self, title: str, node_names: list[str]) -> None:
        self._title = title
        self._pipeline_nodes = [(n, "idle") for n in node_names]
        self.refresh()

    def set_active(self, name: str) -> None:
        self._pipeline_nodes = [
            (n, "active" if n == name else ("done" if s == "active" else s))
            for n, s in self._pipeline_nodes
        ]
        self._start_spinner()
        self.refresh()

    def set_done(self, name: str) -> None:
        self._pipeline_nodes = [(n, "done" if n == name else s) for n, s in self._pipeline_nodes]
        if not any(s == "active" for _, s in self._pipeline_nodes):
            self._stop_spinner()
        self.refresh()

    def set_error(self, name: str) -> None:
        self._pipeline_nodes = [(n, "error" if n == name else s) for n, s in self._pipeline_nodes]
        if not any(s == "active" for _, s in self._pipeline_nodes):
            self._stop_spinner()
        self.refresh()

    def reset(self) -> None:
        self._stop_spinner()
        self._pipeline_nodes = [(n, "idle") for n, _ in self._pipeline_nodes]
        self.refresh()

    def render(self):
        if not self._pipeline_nodes:
            body = Text(justify="left")
            s = self._stats
            if s:
                pending = s["pending"]
                pending_style = "bold yellow" if pending > 0 else "dim"
                body.append("\n")
                body.append("  Emails ingested  ", style="dim")
                body.append(f"{s['emails']}\n", style="white")
                body.append("  Items extracted  ", style="dim")
                body.append(f"{s['items']}\n", style="white")
                body.append("  Digested         ", style="dim")
                body.append(f"{s['digested']}\n", style="green")
                body.append("  Pending digest   ", style="dim")
                body.append(f"{pending}\n", style=pending_style)
                body.append("\n")
                if s["last_ingest"]:
                    ts = s["last_ingest"][:16].replace("T", " ")
                    body.append("  Last ingest   ", style="dim")
                    body.append(f"{ts}\n", style="dim white")
                if s["last_digest"]:
                    ts = s["last_digest"][:16].replace("T", " ")
                    body.append("  Last digest   ", style="dim")
                    body.append(f"{ts}\n", style="dim white")
                body.append("\n")
                body.append("  i  ingest   d  digest   q  quit", style="dim")
            else:
                body.append("\n  No data yet\n\n  i  ingest   d  digest   q  quit", style="dim")
            return body

        body = Text(justify="left")
        body.append("  │\n", style="dim")

        for i, (name, status) in enumerate(self._pipeline_nodes):
            if status == "active":
                icon = _SPINNER_FRAMES[self._frame]
                icon_style = "bold cyan"
            else:
                icon, icon_style = _STATUS_ICON.get(status, _STATUS_ICON["idle"])

            body.append(f"  {icon}", style=icon_style)

            if status == "active":
                body.append(f"  {name}\n", style="bold white")
            elif status == "done":
                body.append(f"  {name}\n", style="green")
            elif status == "error":
                body.append(f"  {name}\n", style="red")
            else:
                body.append(f"  {name}\n", style="dim")

            if i < len(self._pipeline_nodes) - 1:
                body.append("  │\n", style="dim")

        body.append("  │\n", style="dim")

        return body


# --- App ---------------------------------------------------------------------

class HypeHoundApp(App):
    CSS = """
    Horizontal {
        height: 1fr;
    }
    #log-col {
        width: 2fr;
        height: 100%;
        padding: 0;
    }
    RichLog {
        height: 100%;
        border: solid $panel;
        padding: 0 1;
    }
    #graph-col {
        width: 1fr;
        height: 100%;
        padding: 0;
    }
    """

    BINDINGS = [
        Binding("i", "run_ingest", "Ingest", show=True),
        Binding("d", "run_digest", "Digest", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    TITLE = "HypeHound"
    SUB_TITLE = "newsletter ingestor & digest engine"

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="log-col"):
                yield RichLog(id="pipeline-log", highlight=True, markup=True, wrap=True)
            with Vertical(id="graph-col"):
                yield PipelineGraphWidget(id="pipeline-graph")
        yield Footer()

    def on_mount(self) -> None:
        log_widget = self.query_one("#pipeline-log", RichLog)
        handler = RichLogHandler(log_widget)
        handler.setFormatter(logging.Formatter("%(message)s"))
        attach_textual_handler(handler)
        self._refresh_stats()
        log.info(
            "[bold cyan]HypeHound ready.[/bold cyan]  "
            "Press [bold]i[/bold] to ingest · [bold]d[/bold] for digest"
        )

    def _refresh_stats(self) -> None:
        try:
            stats = get_stats()
            self.query_one("#pipeline-graph", PipelineGraphWidget).set_stats(stats)
        except Exception:
            pass

    def _make_deps(self) -> PipelineDeps:
        graph_widget = self.query_one("#pipeline-graph", PipelineGraphWidget)
        return PipelineDeps(
            on_node_start=graph_widget.set_active,
            on_node_done=graph_widget.set_done,
            on_node_error=graph_widget.set_error,
        )

    def action_run_ingest(self) -> None:
        graph_widget = self.query_one("#pipeline-graph", PipelineGraphWidget)
        graph_widget.load("Ingest", INGEST_NODES)
        self.run_worker(self._ingest(), exclusive=True, name="ingest")

    def action_run_digest(self) -> None:
        graph_widget = self.query_one("#pipeline-graph", PipelineGraphWidget)
        graph_widget.load("Digest", DIGEST_NODES)
        self.run_worker(self._digest(), exclusive=True, name="digest")

    async def _ingest(self) -> None:
        from .pipeline import run_ingest
        await run_ingest(deps=self._make_deps())

    async def _digest(self) -> None:
        from .pipeline import run_digest
        end = datetime.now()
        start = end - timedelta(days=7)
        await run_digest(start, end, deps=self._make_deps())

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state == WorkerState.ERROR:
            log.error(f"[red]Worker '{event.worker.name}' failed: {event.worker.error}[/red]")
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR):
            self._refresh_stats()


def launch() -> None:
    HypeHoundApp().run()

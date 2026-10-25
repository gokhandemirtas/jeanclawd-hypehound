import logging
import time
from datetime import datetime, timedelta

import psutil
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, Header, RichLog, Static
from textual.worker import Worker, WorkerState

from .config import settings
from .log import attach_textual_handler, log
from .pipeline import DIGEST_NODES, INGEST_NODES, PODCAST_NODES, PipelineDeps
from .store import get_stats


# --- Log handler -------------------------------------------------------------

class RichLogHandler(logging.Handler):
    def __init__(self, widget: RichLog) -> None:
        super().__init__()
        self.widget = widget

    def emit(self, record: logging.LogRecord) -> None:
        self.widget.write(self.format(record))


# --- Markup helpers ----------------------------------------------------------

_SPINNER_FRAMES = ("◐", "◓", "◑", "◒")


def _bar(fraction: float, width: int = 10) -> str:
    filled = min(width, int(fraction * width))
    empty = width - filled
    if fraction > 0.85:
        color = "#f87171"
    elif fraction > 0.60:
        color = "#facc15"
    else:
        color = "#4ade80"
    return f"[{color}]{'█' * filled}[/{color}][dim]{'░' * empty}[/dim]"


def _model_markup(width: int = 24) -> str:
    model = settings.inference_model
    base = settings.inference_base_url
    tts = settings.voicebox_url

    sep = f"[dim]{'─' * width}[/dim]"
    label_w = 10
    val_w = max(4, width - label_w)

    def row(label: str, val: str) -> str:
        if len(val) > val_w:
            val = val[:val_w - 1] + "…"
        return f"[dim]{label:<{label_w}}[/dim][#22d3ee]{val}[/#22d3ee]"

    return "\n".join([
        "[bold #22d3ee]INFERENCE[/bold #22d3ee]",
        sep,
        row("model", model),
        row("endpoint", base.removeprefix("http://").removeprefix("https://")),
        row("tts", tts.removeprefix("http://").removeprefix("https://")),
    ])


def _resource_markup(width: int = 24) -> str:
    cpu_pct = psutil.cpu_percent()
    mem = psutil.virtual_memory()
    mem_used_gb = mem.used / (1024 ** 3)
    mem_total_gb = mem.total / (1024 ** 3)
    mem_pct = mem.percent / 100

    bar_w = max(6, width - 12)
    sep = f"[dim]{'─' * width}[/dim]"

    cpu_bar = _bar(cpu_pct / 100, bar_w)
    mem_bar = _bar(mem_pct, bar_w)

    cpu_label = f"[dim]CPU  [/dim]{cpu_bar}[dim] {cpu_pct:4.0f}%[/dim]"
    mem_label = f"[dim]RAM  [/dim]{mem_bar}[dim] {mem_used_gb:.1f}G[/dim]"

    return "\n".join([
        "[bold #22d3ee]RESOURCES[/bold #22d3ee]",
        sep,
        cpu_label,
        mem_label,
    ])


def _pipeline_markup(
    title: str,
    nodes: list[str],
    current: str,
    completed: set[str],
    error: bool,
    node_times: dict[str, float],
    spinner_frame: int,
    width: int = 24,
    progress: tuple[int, int] | None = None,
    node_labels: dict[str, str] | None = None,
    current_start: float = 0.0,
) -> str:
    if not nodes:
        return "[bold #22d3ee]PIPELINE[/bold #22d3ee]  [dim]idle[/dim]"

    exec_indicator = "  [bold #facc15]● EXEC[/bold #facc15]" if current != "idle" else ""
    sep = f"[dim]{'─' * width}[/dim]"
    lines = [f"[bold #22d3ee]{title.upper()}[/bold #22d3ee]{exec_indicator}", sep]
    if progress is not None:
        done, total = progress
        pad = max(1, width - len(f"{done} / {total}"))
        lines.append(f"[dim]{' ' * pad}{done} / {total}[/dim]")

    for i, name in enumerate(nodes):
        is_current = name == current
        is_done = name in completed
        is_error = is_current and error

        if is_error:
            icon, style = "◎", "#f87171"
        elif is_current:
            icon, style = _SPINNER_FRAMES[spinner_frame % len(_SPINNER_FRAMES)], "#facc15"
        elif is_done:
            icon, style = "●", "#4ade80"
        else:
            icon, style = "○", "dim"

        suffix = f" {node_labels[name]}" if node_labels and name in node_labels else ""
        label = f"{icon} {name}{suffix}"
        if is_current and not is_error:
            live = time.monotonic() - current_start
            dur = f"{live:.1f}s" if live < 10 else f"{live:.0f}s"
            pad = max(1, width - len(label) - len(dur))
            lines.append(f"[{style}]{label}[/{style}][dim]{' ' * pad}{dur}[/dim]")
        elif name in node_times:
            elapsed = node_times[name]
            dur = f"{elapsed:.1f}s" if elapsed < 10 else f"{elapsed:.0f}s"
            pad = max(1, width - len(label) - len(dur))
            lines.append(f"[{style}]{label}[/{style}][dim]{' ' * pad}{dur}[/dim]")
        else:
            lines.append(f"[{style}]{label}[/{style}]")

        if i < len(nodes) - 1:
            conn_style = "#4ade80" if is_done else ("dim" if not is_current else "#facc15")
            lines.append(f"[{conn_style}]│[/{conn_style}]")

    return "\n".join(lines)


def _stats_markup(width: int = 24) -> str:
    try:
        s = get_stats()
    except Exception:
        return ""
    sep = f"[dim]{'─' * width}[/dim]"
    pending = s["pending"]
    pending_style = "bold #facc15" if pending > 0 else "dim"
    lines = [
        "[bold #22d3ee]DB[/bold #22d3ee]",
        sep,
        f"[dim]newsletters [/dim][#22d3ee]{s['emails']}[/#22d3ee]",
        f"[dim]news items  [/dim][#22d3ee]{s['items']}[/#22d3ee]",
        f"[dim]in digest   [/dim][#22d3ee]{s['digested']}[/#22d3ee]",
        f"[dim]undigested  [/dim][{pending_style}]{pending}[/{pending_style}]",
    ]
    if s["last_ingest"]:
        ts = s["last_ingest"][:16].replace("T", " ")
        lines.append(f"[dim]last ingest  {ts}[/dim]")
    if s["last_digest"]:
        ts = s["last_digest"][:16].replace("T", " ")
        lines.append(f"[dim]last digest  {ts}[/dim]")
    return "\n".join(lines)


# --- App ---------------------------------------------------------------------

class HypeHoundApp(App):
    CSS = """
    Screen {
        background: #0f111a;
        color: #f1f5f9;
    }
    Header {
        height: 1;
        background: #141622;
        color: #f1f5f9;
        border-bottom: solid #24283b;
    }
    Footer {
        height: 1;
        background: #141622;
        color: #94a3b8;
        border-top: solid #24283b;
    }
    Horizontal {
        height: 1fr;
    }
    #log-col {
        width: 2fr;
        height: 100%;
    }
    RichLog {
        height: 100%;
        border: none;
        padding: 0 1;
        background: #0f111a;
        scrollbar-color: #24283b #0f111a;
    }
    #right-panel {
        width: 25%;
        height: 100%;
        border-left: tall #24283b;
        background: #141624;
    }
    #right-info {
        width: 1fr;
        height: auto;
        padding: 1 2;
    }
    #model-panel, #resource-panel, #pipeline-panel, #stats-panel {
        width: 100%;
        height: auto;
        padding: 0 0 1 0;
    }
    """

    BINDINGS = [
        Binding("i", "run_ingest", "Ingest", show=True),
        Binding("d", "run_digest", "Digest", show=True),
        Binding("p", "run_podcast", "Podcast", show=True),
        Binding("r", "run_all", "Run all", show=True),
        Binding("ctrl+x", "reset_db", "Reset DB", show=True),
        Binding("q", "quit", "Quit", show=True),
    ]

    TITLE = "HypeHound"
    SUB_TITLE = "newsletter ingestor & digest engine"

    def __init__(self) -> None:
        super().__init__()
        self._pipeline_title = "PIPELINE"
        self._pipeline_nodes: list[str] = []
        self._pipeline_current = "idle"
        self._pipeline_completed: set[str] = set()
        self._pipeline_error = False
        self._node_times: dict[str, float] = {}
        self._node_start_time: float = 0.0
        self._pipeline_progress: tuple[int, int] | None = None
        self._spinner_frame = 0
        self._confirming_reset = False
        self._node_labels: dict[str, str] = {}

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Horizontal():
            with Vertical(id="log-col"):
                yield RichLog(id="pipeline-log", highlight=False, markup=True, wrap=True)
            with Vertical(id="right-panel"):
                with Vertical(id="right-info"):
                    yield Static("", id="model-panel", markup=True)
                    yield Static("", id="resource-panel", markup=True)
                    yield Static("", id="pipeline-panel", markup=True)
                    yield Static("", id="stats-panel", markup=True)
        yield Footer()

    def on_mount(self) -> None:
        psutil.cpu_percent(interval=None)  # warm up

        log_widget = self.query_one("#pipeline-log", RichLog)
        handler = RichLogHandler(log_widget)
        handler.setFormatter(logging.Formatter("%(message)s"))
        attach_textual_handler(handler)

        self.set_interval(1.0, self._refresh_resources)
        self.set_interval(1.0, self._refresh_model)
        self.set_interval(1.0, self._refresh_stats)
        self.set_interval(0.2, self._refresh_pipeline)
        self.set_interval(0.1, self._tick_spinner)

        self.call_after_refresh(self._refresh_model)
        self.call_after_refresh(self._refresh_resources)
        self.call_after_refresh(self._refresh_pipeline)
        self.call_after_refresh(self._refresh_stats)

        log.info(
            "[bold #22d3ee]HypeHound ready.[/bold #22d3ee]  "
            "[dim]i[/dim] ingest  [dim]d[/dim] digest  [dim]p[/dim] podcast  "
            "[dim]r[/dim] run all  [dim]ctrl+x[/dim] reset  [dim]q[/dim] quit"
        )
        self.run_worker(self._run_health_checks(), exclusive=False, name="health")

    def _right_width(self) -> int:
        try:
            return max(16, self.query_one("#model-panel", Static).size.width)
        except Exception:
            return 24

    def on_resize(self) -> None:
        self._refresh_model()

    def _refresh_model(self) -> None:
        try:
            self.query_one("#model-panel", Static).update(_model_markup(self._right_width()))
        except Exception:
            pass

    def _refresh_resources(self) -> None:
        try:
            self.query_one("#resource-panel", Static).update(_resource_markup(self._right_width()))
        except Exception:
            pass

    def _tick_spinner(self) -> None:
        if self._pipeline_current != "idle":
            self._spinner_frame += 1

    def _refresh_pipeline(self) -> None:
        try:
            self.query_one("#pipeline-panel", Static).update(
                _pipeline_markup(
                    self._pipeline_title,
                    self._pipeline_nodes,
                    self._pipeline_current,
                    self._pipeline_completed,
                    self._pipeline_error,
                    self._node_times,
                    self._spinner_frame,
                    self._right_width(),
                    self._pipeline_progress,
                    self._node_labels,
                    self._node_start_time,
                )
            )
        except Exception:
            pass

    def _refresh_stats(self) -> None:
        try:
            self.query_one("#stats-panel", Static).update(_stats_markup(self._right_width()))
        except Exception:
            pass

    # --- Pipeline node callbacks ---------------------------------------------

    def _node_start(self, name: str) -> None:
        self._pipeline_current = name
        self._pipeline_error = False
        self._node_start_time = time.monotonic()

    def _node_done(self, name: str) -> None:
        elapsed = time.monotonic() - self._node_start_time
        self._node_times[name] = elapsed
        self._pipeline_completed.add(name)
        self._pipeline_current = "idle"

    def _node_error(self, name: str) -> None:
        self._pipeline_error = True
        self._pipeline_current = "idle"

    def _reset_pipeline(self, title: str, nodes: list[str]) -> None:
        self._pipeline_title = title
        self._pipeline_nodes = nodes
        self._pipeline_current = "idle"
        self._pipeline_completed = set()
        self._pipeline_error = False
        self._node_times = {}
        self._pipeline_progress = None
        self._node_labels = {}
        self._spinner_frame = 0

    def _on_progress(self, done: int, total: int) -> None:
        self._pipeline_progress = (done, total)
        self._node_labels["Fetch"] = f"({total})"

    def _make_deps(self) -> PipelineDeps:
        return PipelineDeps(
            on_node_start=self._node_start,
            on_node_done=self._node_done,
            on_node_error=self._node_error,
            on_progress=self._on_progress,
        )

    # --- Actions -------------------------------------------------------------

    def action_run_ingest(self) -> None:
        self._reset_pipeline("Ingest", INGEST_NODES)
        self.run_worker(self._ingest(), exclusive=True, name="ingest")

    def action_run_digest(self) -> None:
        self._reset_pipeline("Digest", DIGEST_NODES)
        self.run_worker(self._digest(), exclusive=True, name="digest")

    def action_run_podcast(self) -> None:
        self._reset_pipeline("Podcast", DIGEST_NODES + PODCAST_NODES)
        self.run_worker(self._podcast(), exclusive=True, name="podcast")

    def action_run_all(self) -> None:
        self._reset_pipeline("Full run", INGEST_NODES + DIGEST_NODES + PODCAST_NODES)
        self.run_worker(self._run_all(), exclusive=True, name="run_all")

    def action_reset_db(self) -> None:
        if self._confirming_reset:
            self._confirming_reset = False
            from .store import reset_db
            reset_db()
            self._reset_pipeline("", [])
            self._refresh_stats()
            log.info("[bold red]Database cleared.[/bold red]")
        else:
            self._confirming_reset = True
            log.info("[yellow]Press ctrl+x again to confirm database reset, any other key to cancel.[/yellow]")

    def on_key(self, event) -> None:
        if self._confirming_reset and event.key != "ctrl+x":
            self._confirming_reset = False
            log.info("[dim]Reset cancelled.[/dim]")

    async def _run_health_checks(self) -> None:
        from .health import run_checks
        log.info("[dim]Running startup health checks…[/dim]")
        results = await run_checks()
        for r in results:
            if r.ok:
                log.info(f"  [green]✓[/green] [bold]{r.service}[/bold]  {r.detail}")
            else:
                log.warning(f"  [red]✗[/red] [bold]{r.service}[/bold]  [red]{r.detail}[/red]")

    async def _ingest(self) -> None:
        from .pipeline import run_ingest
        await run_ingest(deps=self._make_deps())

    async def _digest(self) -> None:
        from .pipeline import run_digest
        end = datetime.now()
        start = end - timedelta(days=7)
        await run_digest(start, end, deps=self._make_deps())

    async def _podcast(self) -> None:
        from .models import Digest, NarratorMode
        from .pipeline import run_podcast
        from .store import get_latest_digest_content
        end = datetime.now()
        start = end - timedelta(days=1)
        digest = None
        raw = get_latest_digest_content()
        if raw:
            try:
                digest = Digest.model_validate_json(raw)
                log.info("[dim]Resuming from latest stored digest[/dim]")
            except Exception as exc:
                log.warning(f"[yellow]Could not load stored digest: {exc} — will re-run digest[/yellow]")
        await run_podcast(start, end, mode=NarratorMode.single, deps=self._make_deps(), digest=digest)

    async def _run_all(self) -> None:
        from .models import NarratorMode
        from .pipeline import run_digest, run_ingest, run_podcast
        deps = self._make_deps()
        await run_ingest(deps=deps)
        end = datetime.now()
        start = end - timedelta(days=1)
        digest_state = await run_digest(start, end, deps=deps)
        if digest_state.digest is not None:
            await run_podcast(start, end, mode=NarratorMode.single, deps=deps, digest=digest_state.digest)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state == WorkerState.ERROR:
            log.error(f"[red]Worker '{event.worker.name}' failed: {event.worker.error}[/red]")
        if event.state in (WorkerState.SUCCESS, WorkerState.ERROR):
            self._pipeline_current = "idle"
            self._refresh_pipeline()
            self._refresh_stats()


def launch() -> None:
    HypeHoundApp().run()

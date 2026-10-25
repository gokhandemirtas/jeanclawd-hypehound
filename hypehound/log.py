import logging
import time
from contextlib import contextmanager
from typing import Generator

from rich.console import Console
from rich.logging import RichHandler

LOG_FILE = "hypehound.log"

console = Console()

_rich_handler = RichHandler(
    console=console,
    rich_tracebacks=True,
    markup=True,
    show_path=False,
    log_time_format="[%H:%M:%S]",
)

_file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
_file_handler.setFormatter(logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s"))

logging.basicConfig(level=logging.DEBUG, format="%(message)s", handlers=[_rich_handler, _file_handler])

log = logging.getLogger("hypehound")


def attach_textual_handler(handler: logging.Handler) -> None:
    """Attach a Textual widget handler so log output is mirrored into the TUI."""
    log.addHandler(handler)


@contextmanager
def step(name: str, show_start: bool = False) -> Generator[None, None, None]:
    """Time a pipeline step and log success or failure with elapsed time."""
    if show_start:
        log.info(f"[cyan]→[/cyan]  {name}")
    start = time.perf_counter()
    try:
        yield
        elapsed = time.perf_counter() - start
        log.info(f"[green]✓[/green]  {name} [dim]({elapsed:.2f}s)[/dim]")
    except Exception as exc:
        elapsed = time.perf_counter() - start
        log.error(f"[red]✗[/red]  {name} [dim]({elapsed:.2f}s)[/dim] — {exc}")
        raise

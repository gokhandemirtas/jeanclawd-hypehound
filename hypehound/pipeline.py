from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

from pydantic import BaseModel, Field
from pydantic_graph import BaseNode, End, GraphBuilder, GraphRunContext

from pathlib import Path

from .classify import classify
from .digest import generate_digest
from .ingest import fetch_new_emails
from .log import log, step
from .models import Digest, NarratorMode, PodcastScript
from .store import mark_processed, save_email, save_items


# --- Deps (observer callbacks injected by the TUI or CLI) --------------------

@dataclass
class PipelineDeps:
    on_node_start: Callable[[str], None] = field(default_factory=lambda: lambda _: None)
    on_node_done: Callable[[str], None] = field(default_factory=lambda: lambda _: None)
    on_node_error: Callable[[str], None] = field(default_factory=lambda: lambda _: None)


# --- State -------------------------------------------------------------------

class PipelineState(BaseModel):
    # Ingest
    raw_emails: list[dict] = Field(default_factory=list)
    current_email: dict | None = None
    current_email_id: int | None = None
    processed_count: int = 0
    failed_count: int = 0

    # Digest
    period_start: datetime = Field(default_factory=lambda: datetime.now() - timedelta(days=1))
    period_end: datetime = Field(default_factory=datetime.now)
    digest: Digest | None = None

    # Podcast (populated when --podcast is requested)
    podcast_script: PodcastScript | None = None
    podcast_mode: NarratorMode = NarratorMode.single
    podcast_json_path: str = "podcast/podcast.json"
    podcast_tts: bool = False
    podcast_audio_path: str = "podcast/podcast.m4a"

    model_config = {"arbitrary_types_allowed": True}


# --- Nodes -------------------------------------------------------------------

@dataclass
class Fetch(BaseNode[PipelineState, PipelineDeps]):
    async def run(self, ctx: GraphRunContext[PipelineState, PipelineDeps]) -> "Pick | End[PipelineState]":
        ctx.deps.on_node_start("Fetch")
        try:
            with step("Fetch emails from Resend"):
                ctx.state.raw_emails = await fetch_new_emails()
            count = len(ctx.state.raw_emails)
            log.info(f"Found [bold]{count}[/bold] new email(s)")
            ctx.deps.on_node_done("Fetch")
            if not count:
                return End(ctx.state)
            return Pick()
        except Exception:
            ctx.deps.on_node_error("Fetch")
            raise


@dataclass
class Pick(BaseNode[PipelineState, PipelineDeps]):
    async def run(self, ctx: GraphRunContext[PipelineState, PipelineDeps]) -> "Save | End[PipelineState]":
        ctx.deps.on_node_start("Pick")
        if not ctx.state.raw_emails:
            total = ctx.state.processed_count + ctx.state.failed_count
            log.info(
                f"[green]Ingest complete[/green] — "
                f"{ctx.state.processed_count}/{total} email(s) processed"
            )
            ctx.deps.on_node_done("Pick")
            return End(ctx.state)

        ctx.state.current_email = ctx.state.raw_emails.pop(0)
        ctx.deps.on_node_done("Pick")
        return Save()


@dataclass
class Save(BaseNode[PipelineState, PipelineDeps]):
    async def run(self, ctx: GraphRunContext[PipelineState, PipelineDeps]) -> "Classify | Pick":
        ctx.deps.on_node_start("Save")
        email = ctx.state.current_email
        subject = email["subject"] or "(no subject)"

        try:
            with step(f"Save · {subject}"):
                ctx.state.current_email_id = save_email(
                    message_id=email["message_id"],
                    sender=email["sender"],
                    subject=subject,
                    received_at=email["received_at"],
                    raw_text=email["text"],
                )
            ctx.deps.on_node_done("Save")
            return Classify()
        except Exception as exc:
            log.error(f"[red]Failed to save '{subject}': {exc} — skipping[/red]")
            ctx.deps.on_node_error("Save")
            ctx.state.failed_count += 1
            return Pick()


@dataclass
class Classify(BaseNode[PipelineState, PipelineDeps]):
    async def run(self, ctx: GraphRunContext[PipelineState, PipelineDeps]) -> "Store | Pick":
        ctx.deps.on_node_start("Classify")
        email = ctx.state.current_email
        subject = email["subject"] or "(no subject)"

        try:
            with step(f"Classify · {subject}"):
                items = await classify(
                    email["text"],
                    email["resolved_urls"],
                    sender=email["sender"],
                    received_at=email["received_at"],
                )
            ctx.state.current_email["items"] = items
            ctx.deps.on_node_done("Classify")
            return Store()
        except Exception as exc:
            log.error(f"[red]Failed to classify '{subject}': {exc} — skipping[/red]")
            ctx.deps.on_node_error("Classify")
            ctx.state.failed_count += 1
            return Pick()


@dataclass
class Store(BaseNode[PipelineState, PipelineDeps]):
    async def run(self, ctx: GraphRunContext[PipelineState, PipelineDeps]) -> Pick:
        ctx.deps.on_node_start("Store")
        email = ctx.state.current_email
        items = email["items"]
        subject = email["subject"] or "(no subject)"

        with step(f"Store {len(items)} item(s) · {subject}"):
            save_items(ctx.state.current_email_id, [i.model_dump() for i in items])
            mark_processed(ctx.state.current_email_id)

        ctx.state.processed_count += 1
        ctx.deps.on_node_done("Store")
        return Pick()


@dataclass
class BuildDigest(BaseNode[PipelineState, PipelineDeps]):
    async def run(self, ctx: GraphRunContext[PipelineState, PipelineDeps]) -> End[PipelineState]:
        ctx.deps.on_node_start("BuildDigest")
        try:
            ctx.state.digest = await generate_digest(ctx.state.period_start, ctx.state.period_end)

            digest = ctx.state.digest
            log.info(f"[green]Digest ready[/green] — {len(digest.entries)} categor(ies)")
            for entry in digest.entries:
                log.info(f"  [cyan]{entry.category}[/cyan] — {len(entry.items)} item(s)")

            ctx.deps.on_node_done("BuildDigest")
            return End(ctx.state)
        except Exception:
            ctx.deps.on_node_error("BuildDigest")
            raise


@dataclass
class GeneratePodcast(BaseNode[PipelineState, PipelineDeps]):
    async def run(self, ctx: GraphRunContext[PipelineState, PipelineDeps]) -> End[PipelineState]:
        ctx.deps.on_node_start("GeneratePodcast")
        try:
            from .podcast import generate_podcast_script

            if ctx.state.digest is None:
                log.error("[red]No digest available — run digest first[/red]")
                ctx.deps.on_node_error("GeneratePodcast")
                return End(ctx.state)

            script = await generate_podcast_script(ctx.state.digest, ctx.state.podcast_mode)
            ctx.state.podcast_script = script

            out = Path(ctx.state.podcast_json_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(script.model_dump_json(indent=2))
            log.info(f"Script saved → [bold cyan]{out}[/bold cyan]")

            if ctx.state.podcast_tts:
                from .tts import synthesize_script
                await synthesize_script(script, Path(ctx.state.podcast_audio_path))

            ctx.deps.on_node_done("GeneratePodcast")
            return End(ctx.state)
        except Exception:
            ctx.deps.on_node_error("GeneratePodcast")
            raise


# --- Graphs ------------------------------------------------------------------

def _build_ingest_graph():
    g = GraphBuilder(
        state_type=PipelineState,
        deps_type=PipelineDeps,
        input_type=Fetch,
        output_type=PipelineState,
    )
    g.add(
        g.edge_from(g.start_node).to(Fetch),
        g.node(Fetch),
        g.node(Pick),
        g.node(Save),
        g.node(Classify),
        g.node(Store),
    )
    return g.build()


def _build_digest_graph():
    g = GraphBuilder(
        state_type=PipelineState,
        deps_type=PipelineDeps,
        input_type=BuildDigest,
        output_type=PipelineState,
    )
    g.add(
        g.edge_from(g.start_node).to(BuildDigest),
        g.node(BuildDigest),
    )
    return g.build()


def _build_podcast_graph():
    g = GraphBuilder(
        state_type=PipelineState,
        deps_type=PipelineDeps,
        input_type=GeneratePodcast,
        output_type=PipelineState,
    )
    g.add(
        g.edge_from(g.start_node).to(GeneratePodcast),
        g.node(GeneratePodcast),
    )
    return g.build()


ingest_graph = _build_ingest_graph()
digest_graph = _build_digest_graph()
podcast_graph = _build_podcast_graph()

INGEST_NODES = ["Fetch", "Pick", "Save", "Classify", "Store"]
DIGEST_NODES = ["BuildDigest"]
PODCAST_NODES = ["GeneratePodcast"]


# --- Entry points ------------------------------------------------------------

async def run_ingest(deps: PipelineDeps | None = None) -> PipelineState:
    state = PipelineState()
    return await ingest_graph.run(state=state, deps=deps or PipelineDeps(), inputs=Fetch())


async def run_digest(period_start: datetime, period_end: datetime, deps: PipelineDeps | None = None) -> PipelineState:
    state = PipelineState(period_start=period_start, period_end=period_end)
    return await digest_graph.run(state=state, deps=deps or PipelineDeps(), inputs=BuildDigest())


async def run_podcast(
    period_start: datetime,
    period_end: datetime,
    mode: NarratorMode = NarratorMode.single,
    tts: bool = False,
    json_path: str = "podcast.json",
    audio_path: str = "podcast.m4a",
    deps: PipelineDeps | None = None,
) -> PipelineState:
    digest_state = await run_digest(period_start, period_end, deps=deps)
    state = PipelineState(
        period_start=period_start,
        period_end=period_end,
        digest=digest_state.digest,
        podcast_mode=mode,
        podcast_tts=tts,
        podcast_json_path=json_path,
        podcast_audio_path=audio_path,
    )
    return await podcast_graph.run(state=state, deps=deps or PipelineDeps(), inputs=GeneratePodcast())

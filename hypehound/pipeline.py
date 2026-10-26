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
from .models import AudioQuality, ContentDensity, Digest, NarratorMode, PodcastScript
from .store import mark_processed, save_email, save_items


# --- Deps (observer callbacks injected by the TUI or CLI) --------------------

@dataclass
class PipelineDeps:
    on_node_start: Callable[[str], None] = field(default_factory=lambda: lambda _: None)
    on_node_done: Callable[[str], None] = field(default_factory=lambda: lambda _: None)
    on_node_error: Callable[[str], None] = field(default_factory=lambda: lambda _: None)
    on_progress: Callable[[int, int], None] = field(default_factory=lambda: lambda _a, _b: None)


# --- State -------------------------------------------------------------------

class PipelineState(BaseModel):
    # Ingest
    raw_emails: list[dict] = Field(default_factory=list)
    current_email: dict | None = None
    current_email_id: int | None = None
    total_emails: int = 0
    processed_count: int = 0
    failed_count: int = 0

    # Digest
    period_start: datetime = Field(default_factory=lambda: datetime.now() - timedelta(days=1))
    period_end: datetime = Field(default_factory=datetime.now)
    digest: Digest | None = None
    digest_id: int | None = None

    # Shared output settings
    content_density: ContentDensity = ContentDensity.standard
    audio_quality: AudioQuality = AudioQuality.high

    # Podcast (populated when --podcast is requested)
    podcast_script: PodcastScript | None = None
    podcast_mode: NarratorMode = NarratorMode.single
    podcast_json_path: str | None = None   # None = auto-generate from period_end
    podcast_tts: bool = False
    podcast_audio_path: str | None = None  # None = auto-generate from period_end
    podcast_audio_resolved: str | None = None  # set after synthesis; read by Publish
    publish_web: bool = False

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
            ctx.state.total_emails = count
            ctx.deps.on_progress(0, count)
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
        done = ctx.state.processed_count + ctx.state.failed_count + 1
        ctx.deps.on_progress(done, ctx.state.total_emails)
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

        if not items:
            preview = (email.get("text") or "")[:120].replace("\n", " ").strip()
            log.warning(
                f"[yellow]No items extracted from '{subject}' — "
                f"LLM found no news content[/yellow]"
                + (f"\n  [dim]{preview}…[/dim]" if preview else "")
            )
            ctx.state.failed_count += 1
            ctx.deps.on_node_done("Store")
            return Pick()

        with step(f"Store {len(items)} item(s) · {subject}"):
            save_items(ctx.state.current_email_id, [i.model_dump() for i in items])
            mark_processed(ctx.state.current_email_id)

        ctx.state.processed_count += 1
        ctx.deps.on_node_done("Store")
        return Pick()


@dataclass
class BuildDigest(BaseNode[PipelineState, PipelineDeps]):
    async def run(self, ctx: GraphRunContext[PipelineState, PipelineDeps]) -> End[PipelineState]:
        ctx.deps.on_node_start("Digest")
        try:
            ctx.state.digest, ctx.state.digest_id = await generate_digest(
                ctx.state.period_start, ctx.state.period_end, density=ctx.state.content_density
            )

            digest = ctx.state.digest
            log.info(f"[green]Digest ready[/green] — {len(digest.entries)} categor(ies)")
            for entry in digest.entries:
                log.info(f"  [cyan]{entry.category}[/cyan] — {len(entry.items)} item(s)")

            ctx.deps.on_node_done("Digest")
            return End(ctx.state)
        except ValueError as exc:
            if "No undigested items" in str(exc):
                log.info("[dim]No undigested items in period — skipping digest[/dim]")
                ctx.deps.on_node_done("Digest")
                return End(ctx.state)
            ctx.deps.on_node_error("Digest")
            raise
        except Exception:
            ctx.deps.on_node_error("Digest")
            raise


def _versioned_path(directory: str, stem: str, suffix: str) -> Path:
    base = Path(directory) / f"{stem}{suffix}"
    if not base.exists():
        return base
    n = 2
    while True:
        candidate = Path(directory) / f"{stem}_v{n}{suffix}"
        if not candidate.exists():
            return candidate
        n += 1


@dataclass
class GeneratePodcast(BaseNode[PipelineState, PipelineDeps]):
    async def run(self, ctx: GraphRunContext[PipelineState, PipelineDeps]) -> "Publish | End[PipelineState]":
        ctx.deps.on_node_start("GeneratePodcast")
        try:
            from .podcast import generate_podcast_script

            if ctx.state.digest is None:
                log.error("[red]No digest available — run digest first[/red]")
                ctx.deps.on_node_error("GeneratePodcast")
                return End(ctx.state)

            stem = ctx.state.period_end.strftime("%Y-%m-%d")
            explicit_json = Path(ctx.state.podcast_json_path) if ctx.state.podcast_json_path else None
            base_json = Path("podcast") / f"{stem}.json"

            # Re-use an existing script (explicit path or base path) to preserve segment cache
            existing_json = explicit_json if (explicit_json and explicit_json.exists()) else (base_json if base_json.exists() else None)
            if existing_json:
                log.info(f"Loading existing script → [bold cyan]{existing_json}[/bold cyan]")
                script = PodcastScript.model_validate_json(existing_json.read_text())
                out = existing_json
                # Restore digest linkage from the persisted script
                if script.digest_id is not None:
                    ctx.state.digest_id = script.digest_id
                    if ctx.state.digest is None:
                        from .store import get_digest_by_id
                        raw = get_digest_by_id(script.digest_id)
                        if raw:
                            ctx.state.digest = Digest.model_validate_json(raw)
            else:
                script = await generate_podcast_script(
                    ctx.state.digest, ctx.state.podcast_mode, density=ctx.state.content_density
                )
                script.digest_id = ctx.state.digest_id
                out = explicit_json or _versioned_path("podcast", stem, ".json")
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_text(script.model_dump_json(indent=2))
                log.info(f"Script saved → [bold cyan]{out}[/bold cyan]")

            ctx.state.podcast_script = script

            if ctx.state.podcast_tts:
                from .tts import synthesize_script
                audio_out = (
                    Path(ctx.state.podcast_audio_path)
                    if ctx.state.podcast_audio_path
                    else _versioned_path("podcast", stem, ".m4a")
                )
                log.info(f"Starting TTS synthesis → [bold cyan]{audio_out}[/bold cyan]  ({len(script.segments)} segment(s))")
                await synthesize_script(script, audio_out, bitrate=ctx.state.audio_quality.bitrate)
                ctx.state.podcast_audio_resolved = str(audio_out)
            else:
                log.info("[dim]TTS disabled — script only[/dim]")

            ctx.deps.on_node_done("GeneratePodcast")
            if ctx.state.publish_web and ctx.state.podcast_audio_resolved:
                return Publish()
            return End(ctx.state)
        except Exception:
            ctx.deps.on_node_error("GeneratePodcast")
            raise


@dataclass
class Publish(BaseNode[PipelineState, PipelineDeps]):
    async def run(self, ctx: GraphRunContext[PipelineState, PipelineDeps]) -> End[PipelineState]:
        ctx.deps.on_node_start("Publish")
        try:
            from .publish import publish_episode

            if not ctx.state.podcast_script or not ctx.state.podcast_audio_resolved:
                log.error("[red]Publish requires a script and synthesized audio[/red]")
                ctx.deps.on_node_error("Publish")
                return End(ctx.state)

            if not ctx.state.digest:
                log.error("[red]Publish requires a digest[/red]")
                ctx.deps.on_node_error("Publish")
                return End(ctx.state)

            publish_episode(
                script=ctx.state.podcast_script,
                digest=ctx.state.digest,
                audio_path=Path(ctx.state.podcast_audio_resolved),
                period_start=ctx.state.period_start,
                period_end=ctx.state.period_end,
            )
            ctx.deps.on_node_done("Publish")
            return End(ctx.state)
        except Exception:
            ctx.deps.on_node_error("Publish")
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
        g.node(Publish),
    )
    return g.build()


ingest_graph = _build_ingest_graph()
digest_graph = _build_digest_graph()
podcast_graph = _build_podcast_graph()

INGEST_NODES = ["Fetch", "Classify"]
DIGEST_NODES = ["Digest"]
PODCAST_NODES = ["GeneratePodcast", "Publish"]


# --- Entry points ------------------------------------------------------------

async def run_ingest(deps: PipelineDeps | None = None) -> PipelineState:
    state = PipelineState()
    return await ingest_graph.run(state=state, deps=deps or PipelineDeps(), inputs=Fetch())


async def run_digest(
    period_start: datetime,
    period_end: datetime,
    density: ContentDensity = ContentDensity.standard,
    deps: PipelineDeps | None = None,
) -> PipelineState:
    state = PipelineState(period_start=period_start, period_end=period_end, content_density=density)
    return await digest_graph.run(state=state, deps=deps or PipelineDeps(), inputs=BuildDigest())


async def run_podcast(
    period_start: datetime,
    period_end: datetime,
    mode: NarratorMode = NarratorMode.dual,
    density: ContentDensity = ContentDensity.standard,
    quality: AudioQuality = AudioQuality.high,
    tts: bool = True,
    json_path: str | None = None,
    audio_path: str | None = None,
    publish: bool = False,
    deps: PipelineDeps | None = None,
    digest: "Digest | None" = None,
) -> PipelineState:
    if digest is None:
        digest_state = await run_digest(period_start, period_end, density=density, deps=deps)
        digest = digest_state.digest
    state = PipelineState(
        period_start=period_start,
        period_end=period_end,
        content_density=density,
        audio_quality=quality,
        digest=digest,
        podcast_mode=mode,
        podcast_tts=tts,
        podcast_json_path=json_path,
        podcast_audio_path=audio_path,
        publish_web=publish,
    )
    return await podcast_graph.run(state=state, deps=deps or PipelineDeps(), inputs=GeneratePodcast())

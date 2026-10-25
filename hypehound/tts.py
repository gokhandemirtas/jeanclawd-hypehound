"""
TTS synthesis via jeanclawd-voicebox (MLX Qwen3-TTS on Apple Silicon).

Start voicebox first:
    cd ../jeanclawd-voicebox && uv run jeanclawd-voicebox

Each PodcastSegment becomes one /synthesize request (stream=false → WAV).
For dual narrator, consecutive same-speaker runs are grouped per request.
Segments are written as WAVs then joined into a final M4A via ffmpeg.

Requires ffmpeg: brew install ffmpeg
"""
import asyncio
import re
import shutil
import struct
import subprocess
import tempfile
import wave
from pathlib import Path

import httpx

from .config import settings
from .log import log, step
from .models import NarratorMode, PodcastScript, PodcastSegment


# --- Voicebox HTTP client ----------------------------------------------------

async def _voicebox_wav(segments: list[dict], speaker: str) -> bytes:
    payload = {"segments": segments, "speaker": speaker, "stream": False}
    async with httpx.AsyncClient(timeout=300.0) as client:
        resp = await client.post(f"{settings.voicebox_url}/synthesize", json=payload)
        resp.raise_for_status()
    return resp.content


# --- Silence generator -------------------------------------------------------

def _write_silence(output: Path, duration_s: float = 0.6, sample_rate: int = 24_000) -> None:
    n_frames = int(duration_s * sample_rate)
    with wave.open(str(output), "w") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(b"\x00\x00" * n_frames)


# --- Segment synthesis -------------------------------------------------------

async def _synthesize_segment(segment: PodcastSegment, mode: NarratorMode, output: Path) -> None:
    """Render one PodcastSegment to a WAV file via voicebox."""
    if mode == NarratorMode.single:
        segs = [{"text": line.text} for line in segment.lines]
        output.write_bytes(await _voicebox_wav(segs, settings.tts_voice_single))
        return

    # Dual: group consecutive lines by same speaker into runs
    runs: list[tuple[str, list[str]]] = []
    for line in segment.lines:
        speaker_key = line.speaker or settings.tts_voice_male.upper()
        if runs and runs[-1][0] == speaker_key:
            runs[-1][1].append(line.text)
        else:
            runs.append((speaker_key, [line.text]))

    voice_map = {
        settings.tts_voice_male.upper(): settings.tts_voice_male,
        settings.tts_voice_female.upper(): settings.tts_voice_female,
    }

    if len(runs) == 1:
        segs = [{"text": t} for t in runs[0][1]]
        output.write_bytes(await _voicebox_wav(segs, voice_map.get(runs[0][0], settings.tts_voice_male)))
        return

    # Multiple speaker alternations — synthesize each run, then concat
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        run_paths: list[Path] = []
        for j, (speaker_key, texts) in enumerate(runs):
            run_path = tmp_dir / f"run_{j:03d}.wav"
            segs = [{"text": t} for t in texts]
            run_path.write_bytes(await _voicebox_wav(segs, voice_map.get(speaker_key, settings.tts_voice_male)))
            run_paths.append(run_path)
        _concat_to(run_paths, output)


# --- ffmpeg helpers ----------------------------------------------------------

_FFMPEG_CHUNK = 64  # max inputs per ffmpeg call (well under OS open-file limit)


def _ffmpeg_concat(inputs: list[Path], output: Path, final_encode: bool) -> None:
    """Single ffmpeg concat call; output is always WAV unless final_encode=True (M4A)."""
    codec = ["-c:a", "aac", "-b:a", "64k", "-ac", "1", "-movflags", "+faststart"] if final_encode else ["-c:a", "pcm_s16le"]
    input_args = []
    for p in inputs:
        input_args += ["-i", str(p)]
    n = len(inputs)
    filter_complex = "".join(f"[{i}:a]" for i in range(n)) + f"concat=n={n}:v=0:a=1[out]"
    result = subprocess.run(
        ["ffmpeg", "-y", *input_args, "-filter_complex", filter_complex, "-map", "[out]", *codec, str(output)],
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed (exit {result.returncode}):\n{result.stderr.decode(errors='replace')}")


def _concat_to(inputs: list[Path], output: Path) -> None:
    """Concat audio files; uses chunked progressive stitching to stay under OS open-file limits."""
    if len(inputs) == 1:
        shutil.copy(inputs[0], output)
        return

    if len(inputs) <= _FFMPEG_CHUNK:
        _ffmpeg_concat(inputs, output, final_encode=output.suffix.lower() == ".m4a")
        return

    # Progressive: reduce in chunks of _FFMPEG_CHUNK until one file remains
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        current = list(inputs)
        pass_num = 0
        while len(current) > _FFMPEG_CHUNK:
            next_pass: list[Path] = []
            for chunk_idx in range(0, len(current), _FFMPEG_CHUNK):
                chunk = current[chunk_idx: chunk_idx + _FFMPEG_CHUNK]
                if len(chunk) == 1:
                    next_pass.append(chunk[0])
                    continue
                merged = tmp_dir / f"pass{pass_num}_{chunk_idx:04d}.wav"
                _ffmpeg_concat(chunk, merged, final_encode=False)
                next_pass.append(merged)
            current = next_pass
            pass_num += 1
        _ffmpeg_concat(current, output, final_encode=output.suffix.lower() == ".m4a")


# --- Public API --------------------------------------------------------------

async def synthesize_script(script: PodcastScript, output_path: Path) -> Path:
    """
    Render a PodcastScript to an M4A audio file using voicebox.

    Segment WAVs are written to <output_stem>_segments/ alongside the output
    file so they persist across runs. Already-synthesized segments are skipped,
    allowing resume after a crash.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Strip _vN suffix so all versions share the same segments directory
    base_stem = re.sub(r"_v\d+$", "", output_path.stem)
    segments_dir = output_path.parent / f"{base_stem}_segments"
    segments_dir.mkdir(exist_ok=True)

    n_total = len(script.segments)
    segment_paths: list[Path] = []
    skipped = 0
    for i, segment in enumerate(script.segments):
        seg_path = segments_dir / f"segment_{i:03d}.wav"
        if seg_path.exists():
            skipped += 1
        else:
            with step(f"TTS segment {i + 1}/{n_total}", show_start=True):
                await _synthesize_segment(segment, script.mode, seg_path)
        segment_paths.append(seg_path)

    if skipped:
        log.info(f"[dim]Skipped {skipped}/{n_total} already-synthesized segment(s)[/dim]")

    log.info(f"Stitching {n_total} segment(s) → [bold cyan]{output_path}[/bold cyan]")
    silence_path = segments_dir / "silence.wav"
    _write_silence(silence_path)
    interleaved: list[Path] = []
    if settings.podcast_intro:
        intro = Path(settings.podcast_intro)
        if intro.exists():
            interleaved.append(intro)
        else:
            log.warning(f"podcast_intro not found, skipping: {intro}")
    for i, p in enumerate(segment_paths):
        interleaved.append(p)
        if i < len(segment_paths) - 1:
            interleaved.append(silence_path)

    with step("Join and encode audio"):
        await asyncio.get_event_loop().run_in_executor(None, _concat_to, interleaved, output_path)

    log.info(f"Podcast saved → [bold cyan]{output_path}[/bold cyan]  (segments in {segments_dir})")
    return output_path

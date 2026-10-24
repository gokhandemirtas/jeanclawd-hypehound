"""
TTS synthesis via jeanclawd-voicebox (MLX Qwen3-TTS on Apple Silicon).

Start voicebox first:
    cd ../jeanclawd-voicebox && uv run jeanclawd-voicebox

Each PodcastSegment becomes one /synthesize request (stream=false → WAV).
For dual narrator, consecutive same-speaker runs are grouped per request.
Segments are written as WAVs then joined into a final M4A via ffmpeg.

Requires ffmpeg: brew install ffmpeg
"""
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
        speaker_key = line.speaker or "ALEX"
        if runs and runs[-1][0] == speaker_key:
            runs[-1][1].append(line.text)
        else:
            runs.append((speaker_key, [line.text]))

    voice_map = {"ALEX": settings.tts_voice_male, "SARA": settings.tts_voice_female}

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

def _concat_to(inputs: list[Path], output: Path) -> None:
    """Concat audio files, re-encoding to match output format."""
    if len(inputs) == 1:
        shutil.copy(inputs[0], output)
        return
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        for p in inputs:
            f.write(f"file '{p}'\n")
        list_path = Path(f.name)
    try:
        ext = output.suffix.lower()
        codec = ["-c:a", "aac", "-b:a", "64k", "-ac", "1"] if ext == ".m4a" else ["-c:a", "copy"]
        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(list_path), *codec, str(output)],
            check=True,
            capture_output=True,
        )
    finally:
        list_path.unlink(missing_ok=True)


# --- Public API --------------------------------------------------------------

async def synthesize_script(script: PodcastScript, output_path: Path) -> Path:
    """
    Render a PodcastScript to an M4A audio file using voicebox.

    Each PodcastSegment is a self-contained 1–2 minute chunk synthesized
    independently, then joined with a short silence gap.
    """
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        segment_paths: list[Path] = []

        for i, segment in enumerate(script.segments):
            seg_path = tmp_dir / f"segment_{i:03d}.wav"
            with step(f"TTS segment {i + 1}/{len(script.segments)}"):
                await _synthesize_segment(segment, script.mode, seg_path)
            segment_paths.append(seg_path)

        # Interleave with 0.6s silence for natural pacing between segments
        silence_path = tmp_dir / "silence.wav"
        _write_silence(silence_path)
        interleaved: list[Path] = []
        for i, p in enumerate(segment_paths):
            interleaved.append(p)
            if i < len(segment_paths) - 1:
                interleaved.append(silence_path)

        with step("Join and encode audio"):
            _concat_to(interleaved, output_path)

    log.info(f"Podcast saved → [bold cyan]{output_path}[/bold cyan]")
    return output_path

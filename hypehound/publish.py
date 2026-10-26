"""
Publish step: generates a self-contained episode web page.

Given a PodcastScript + its synthesized audio, produces:
  web/episodes/YYYY-MM-DD/
    index.html   — self-contained page (timing JSON inlined)
    episode.m4a  — copy of the synthesized audio

Timing is derived from the per-segment WAV files written by tts.py, so
it reflects actual audio durations rather than word-count estimates.
"""
import html
import json
import re
import subprocess
import wave
from datetime import datetime
from pathlib import Path

from .config import settings
from .log import log, step
from .models import Digest, NarratorMode, PodcastScript

_GITHUB_RELEASE_FALLBACK = "episode.m4a"


# ---------------------------------------------------------------------------
# Duration helpers
# ---------------------------------------------------------------------------

def _wav_duration_s(path: Path) -> float:
    with wave.open(str(path), "r") as wf:
        return wf.getnframes() / wf.getframerate()


def _media_duration_s(path: Path) -> float:
    """Duration of any media file via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "quiet",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return 0.0
    return float(result.stdout.strip())


# ---------------------------------------------------------------------------
# GitHub release upload
# ---------------------------------------------------------------------------

def _upload_to_github_release(audio_path: Path, stem: str) -> str:
    """
    Upload audio to a per-episode GitHub release and return the download URL.
    Falls back to the relative path "episode.m4a" if gh is unavailable or fails.
    """
    tag = f"episode-{stem}"

    # Resolve repo slug from remote
    repo_result = subprocess.run(
        ["gh", "repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"],
        capture_output=True, text=True,
    )
    if repo_result.returncode != 0:
        log.warning("[yellow]gh not available — skipping GitHub release upload[/yellow]")
        return _GITHUB_RELEASE_FALLBACK

    repo = repo_result.stdout.strip()

    # Create release (idempotent — ignore error if tag already exists)
    subprocess.run(
        [
            "gh", "release", "create", tag,
            "--repo", repo,
            "--title", f"Episode {stem}",
            "--notes", f"Audio for HypeHound episode {stem}",
        ],
        capture_output=True, text=True,
    )

    # Upload (--clobber replaces existing asset with same name)
    upload = subprocess.run(
        ["gh", "release", "upload", tag, str(audio_path),
         "--repo", repo, "--clobber"],
        capture_output=True, text=True,
    )
    if upload.returncode != 0:
        log.warning(f"[yellow]GitHub release upload failed: {upload.stderr.strip()}[/yellow]")
        return _GITHUB_RELEASE_FALLBACK

    url = f"https://github.com/{repo}/releases/download/{tag}/{audio_path.name}"
    log.info(f"[green]Audio uploaded[/green] → [bold cyan]{url}[/bold cyan]")
    return url


# ---------------------------------------------------------------------------
# Timing manifest
# ---------------------------------------------------------------------------

def build_timing_manifest(script: PodcastScript, audio_path: Path) -> list[dict]:
    """
    Build per-line timing by reading per-segment WAV durations and distributing
    each segment's duration proportionally across its lines by word count.

    This gives per-narrator-turn highlighting rather than per-segment (which can
    span many speaker turns and be 1-3 minutes long).

    Returns [{idx, start, end, speaker, text}, ...] where idx is a sequential
    line index that matches the HTML card's data-idx attribute.
    """
    base_stem = re.sub(r"_v\d+$", "", audio_path.stem)
    segments_dir = audio_path.parent / f"{base_stem}_segments"
    silence_s = 0.6  # must match tts._write_silence duration

    cursor_s = 0.0
    if settings.podcast_intro:
        intro = Path(settings.podcast_intro)
        if intro.exists():
            cursor_s += _media_duration_s(intro)

    timing: list[dict] = []
    line_idx = 0

    for i, segment in enumerate(script.segments):
        seg_path = segments_dir / f"segment_{i:03d}.wav"
        if not seg_path.exists():
            log.warning(f"[yellow]Timing: segment_{i:03d}.wav not found — skipped[/yellow]")
            # Still advance line_idx so HTML card indices stay consistent
            line_idx += len(segment.lines)
            continue

        seg_dur = _wav_duration_s(seg_path)
        lines = segment.lines or []
        total_words = sum(max(len(l.text.split()), 1) for l in lines) or 1

        for line in lines:
            line_words = max(len(line.text.split()), 1)
            line_dur = seg_dur * (line_words / total_words)
            timing.append({
                "idx": line_idx,
                "start": round(cursor_s, 3),
                "end": round(cursor_s + line_dur, 3),
                "speaker": line.speaker,
                "text": line.text,
            })
            cursor_s += line_dur
            line_idx += 1

        cursor_s += silence_s

    return timing


# ---------------------------------------------------------------------------
# HTML rendering helpers
# ---------------------------------------------------------------------------

def _h(s: str | None) -> str:
    return html.escape(s or "")


def _fmt_time(s: float) -> str:
    m = int(s) // 60
    sec = int(s) % 60
    return f"{m:02d}:{sec:02d}"


_CATEGORY_COLORS = [
    "#00ff66",  # green
    "#00e5ff",  # cyan
    "#ffb830",  # amber
    "#ff007f",  # magenta
    "#a78bfa",  # purple
]


def _speaker_badge(speaker: str | None) -> str:
    if not speaker:
        return '<span class="px-2 py-0.5 text-[10px] font-space font-bold bg-[#14141a] text-[#8ba28f] border border-[#2c332e]">NARRATOR</span>'
    su = speaker.upper()
    if "VIVIAN" in su:
        return f'<span class="px-2 py-0.5 text-[10px] font-space font-bold bg-[#1d141e] text-[#f472b6] border border-[#44233e]">{_h(speaker)}</span>'
    if "AIDEN" in su:
        return f'<span class="px-2 py-0.5 text-[10px] font-space font-bold bg-[#14141a] text-[#8ba28f] border border-[#2c332e]">{_h(speaker)}</span>'
    return f'<span class="px-2 py-0.5 text-[10px] font-space font-bold bg-[#141820] text-[#93c5fd] border border-[#1e3a5f]">{_h(speaker)}</span>'


# ---------------------------------------------------------------------------
# Main HTML renderer
# ---------------------------------------------------------------------------

def _render_html(
    script: PodcastScript,
    digest: Digest,
    timing: list[dict],
    sources: list[dict],
    audio_url: str = "episode.m4a",
) -> str:
    timing_by_idx = {t["idx"]: t for t in timing}

    # ---- Script cards (one card per line for per-narrator-turn highlighting) -
    script_parts: list[str] = []
    line_idx = 0
    for segment in script.segments:
        for line in (segment.lines or []):
            t = timing_by_idx.get(line_idx, {})
            start_s = t.get("start", 0.0)
            end_s = t.get("end", 0.0)
            badge = _speaker_badge(line.speaker)
            script_parts.append(
                f'          <div class="bg-[#1f1f25] border border-[#2d2d38] p-4 hover:border-[#404052]'
                f' transition-colors group segment" data-idx="{line_idx}" data-start="{start_s}" data-end="{end_s}">\n'
                f'            <div class="flex items-center justify-between mb-2">\n'
                f'              <div class="flex items-center space-x-2">\n'
                f'                {badge}\n'
                f'                <span class="text-[11px] text-[#5d6575] font-mono">[{_fmt_time(start_s)} – {_fmt_time(end_s)}]</span>\n'
                f'              </div>\n'
                f'              <span class="text-[10px] text-[#525968] group-hover:text-[#00ff66] font-mono">SEEK »</span>\n'
                f'            </div>\n'
                f'            <p class="text-sm text-[#e4e1e9] leading-relaxed font-mono">{_h(line.text)}</p>\n'
                f'          </div>'
            )
            line_idx += 1
    script_html = "\n".join(script_parts)

    # ---- Digest categories --------------------------------------------------
    digest_parts: list[str] = []
    total_items = 0
    all_citations: list[dict] = []
    seen_urls: set[str] = set()

    for ci, entry in enumerate(digest.entries):
        color = _CATEGORY_COLORS[ci % len(_CATEGORY_COLORS)]
        story_parts: list[str] = []
        for item in entry.items:
            total_items += 1
            cite_html = ""
            if item.citations:
                link_parts: list[str] = []
                for c in item.citations:
                    url = c.url or ""
                    title = c.title or url
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        all_citations.append({"url": url, "title": title})
                    if url:
                        link_parts.append(
                            f'<a href="{_h(url)}" target="_blank" rel="noopener"'
                            f' class="text-[#00ff66] underline hover:text-[#7dffb2]">'
                            f'{_h((title or url)[:50])} ↗</a>'
                        )
                if link_parts:
                    cite_html = (
                        '<div class="mt-2 pt-2 border-t border-[#292934] text-[10px]'
                        ' text-[#727a8c] flex flex-wrap gap-2">'
                        + " ".join(link_parts)
                        + "</div>"
                    )
            story_parts.append(
                f'              <article class="bg-[#1f1f25] border border-[#2d2d38] p-3 text-xs">\n'
                f'                <h3 class="font-medium text-[#e4e1e9] text-sm leading-snug">{_h(item.headline)}</h3>\n'
                f'                <p class="mt-1.5 text-[#b9ccb5] text-[11px] leading-relaxed">{_h(item.summary)}</p>\n'
                f'                {cite_html}\n'
                f'              </article>'
            )
        n_stories = len(entry.items)
        digest_parts.append(
            f'          <div>\n'
            f'            <div class="flex items-center justify-between mb-2.5 pb-1 border-b border-[#252533]">\n'
            f'              <span class="text-xs font-bold font-space tracking-wide flex items-center gap-1.5" style="color:{color}">\n'
            f'                <span class="w-1.5 h-1.5 inline-block" style="background:{color}"></span>\n'
            f'                {_h(entry.category.upper())}\n'
            f'              </span>\n'
            f'              <span class="text-[10px] bg-[#111116] px-1.5 py-0.5 text-[#8890a2] border border-[#252536]">'
            f'{n_stories} {"STORY" if n_stories == 1 else "STORIES"}</span>\n'
            f'            </div>\n'
            f'            <div class="space-y-3.5">\n'
            + "\n".join(story_parts)
            + "\n            </div>\n          </div>"
        )
    digest_html = "\n".join(digest_parts)

    # ---- Sources ------------------------------------------------------------
    sources_parts: list[str] = []
    for s in sources:
        date_str = (s.get("received_at") or "")[:16].replace("T", " ")
        sources_parts.append(
            f'          <div class="px-3 py-2 flex items-center justify-between hover:bg-[#1a1a24] transition-colors">\n'
            f'            <div class="flex flex-col">\n'
            f'              <span class="text-white font-medium text-xs">{_h(s.get("sender", ""))}</span>\n'
            f'              <span class="text-[#8890a2] text-[10px]">{_h(s.get("subject", ""))}</span>\n'
            f'            </div>\n'
            f'            <span class="text-[#00ff66] font-mono text-[10px]">{_h(date_str)}</span>\n'
            f'          </div>'
        )
    sources_html = "\n".join(sources_parts)

    # ---- All links ----------------------------------------------------------
    urls_parts: list[str] = []
    for ui, c in enumerate(all_citations, 1):
        urls_parts.append(
            f'          <div class="truncate">'
            f'<span class="text-[#596175]">{ui:02d}.</span> '
            f'<a href="{_h(c["url"])}" target="_blank" rel="noopener"'
            f' class="text-[#00ff66] hover:underline">{_h(c["url"])}</a>'
            f'<span class="text-[#7c8396] text-[10px] ml-1">({_h((c["title"] or "")[:60])})</span>'
            f'</div>'
        )
    urls_html = "\n".join(urls_parts)

    # ---- Meta ---------------------------------------------------------------
    ep_date = digest.period_end.strftime("%-d %b %Y")
    ep_tab_date = digest.period_end.strftime("%d %b %Y").upper()
    n_nl = len(sources)
    nl_label = "newsletter" if n_nl == 1 else "newsletters"
    story_label = "story" if total_items == 1 else "stories"
    total_dur = timing[-1]["end"] if timing else 0.0
    total_dur_fmt = _fmt_time(total_dur)
    timing_json = json.dumps(timing, separators=(",", ":"))

    # ---- Player JS ----------------------------------------------------------
    player_js = f"""\
(function () {{
  var TIMING = {timing_json};
  var audio = document.getElementById('player');
  var els = Array.from(document.querySelectorAll('.segment[data-idx]'));
  var lastActive = -1;

  function fmtTime(s) {{
    var m = Math.floor(s / 60), sec = Math.floor(s % 60);
    return (m < 10 ? '0' : '') + m + ':' + (sec < 10 ? '0' : '') + sec;
  }}

  // Play / Pause
  var playBtn  = document.getElementById('playPauseBtn');
  var playIcon = document.getElementById('playIcon');
  var pauseIcon = document.getElementById('pauseIcon');
  playBtn.addEventListener('click', function () {{
    if (audio.paused) {{ audio.play(); }} else {{ audio.pause(); }}
  }});
  audio.addEventListener('play',  function () {{ playIcon.classList.add('hidden'); pauseIcon.classList.remove('hidden'); }});
  audio.addEventListener('pause', function () {{ playIcon.classList.remove('hidden'); pauseIcon.classList.add('hidden'); }});

  // Duration label
  audio.addEventListener('loadedmetadata', function () {{
    document.getElementById('totalTime').textContent = fmtTime(audio.duration);
  }});

  // Time + progress + segment sync
  audio.addEventListener('timeupdate', function () {{
    var t = audio.currentTime, dur = audio.duration || 1;
    document.getElementById('currentTime').textContent = fmtTime(t);
    var pct = (t / dur) * 100;
    document.getElementById('progressBar').style.width = pct + '%';
    document.getElementById('progressHandle').style.left = 'calc(' + pct + '% - 5px)';

    var activeIdx = -1;
    for (var i = 0; i < TIMING.length; i++) {{
      if (t >= TIMING[i].start && t < TIMING[i].end) {{ activeIdx = TIMING[i].idx; break; }}
    }}
    if (activeIdx === lastActive) return;
    lastActive = activeIdx;
    var syncEl = document.getElementById('syncLine');
    if (syncEl) syncEl.textContent = activeIdx >= 0
      ? 'SYNCED · PARAGRAPH #' + String(activeIdx + 1).padStart(2, '0')
      : 'WAITING FOR PLAYBACK';
    els.forEach(function (el) {{
      var isActive = parseInt(el.dataset.idx, 10) === activeIdx;
      el.classList.toggle('active', isActive);
      if (isActive) el.scrollIntoView({{ behavior: 'smooth', block: 'nearest' }});
    }});
  }});

  // Scrubber click
  var container = document.getElementById('progressBarContainer');
  container.addEventListener('click', function (e) {{
    var rect = container.getBoundingClientRect();
    audio.currentTime = ((e.clientX - rect.left) / rect.width) * audio.duration;
  }});

  // Segment click → seek
  els.forEach(function (el) {{
    el.addEventListener('click', function () {{
      var idx = parseInt(el.dataset.idx, 10);
      var entry = TIMING.find(function (s) {{ return s.idx === idx; }});
      if (entry) audio.currentTime = entry.start;
    }});
  }});

  // Skip buttons
  document.getElementById('skipBack').addEventListener('click', function () {{
    audio.currentTime = Math.max(0, audio.currentTime - 15);
  }});
  document.getElementById('skipFwd').addEventListener('click', function () {{
    audio.currentTime = Math.min(audio.duration || 0, audio.currentTime + 30);
  }});

  // Volume
  var volSlider = document.getElementById('volSlider');
  if (volSlider) {{
    volSlider.addEventListener('input', function () {{
      audio.volume = volSlider.value / 100;
      var d = document.getElementById('volDisplay');
      if (d) d.textContent = volSlider.value + '%';
    }});
  }}
}}());"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{_h(script.title)}</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:ital,wght@0,300;0,400;0,500;0,700;1,400&family=Press+Start+2P&family=Space+Mono:ital,wght@0,400;0,700;1,400&display=swap" rel="stylesheet">
  <link rel="stylesheet" href="/static/style.css">
</head>
<body class="min-h-screen flex flex-col bg-[#131318] text-[#e4e1e9] selection:bg-[#00ff66] selection:text-black">

  <audio id="player" preload="metadata" class="hidden">
    <source src="{_h(audio_url)}" type="audio/mp4">
  </audio>

  <div class="fixed inset-0 crt-overlay z-50 pointer-events-none"></div>

  <!-- STATUS BAR -->
  <header class="border-b border-[#242430] bg-[#0c0c11] text-xs font-mono select-none sticky top-0 z-40">
    <div class="flex items-center justify-between px-4 py-2">
      <div class="flex items-center space-x-4">
        <div class="flex items-center space-x-2 text-[#00ff66]">
          <span class="inline-block w-2 h-2 bg-[#00ff66] pulse-dot"></span>
          <span class="font-bold tracking-wider font-space">[HYPE_HOUND:PODCAST]</span>
        </div>
        <span class="text-[#4f5565] hidden md:inline">|</span>
        <span class="text-[#7c8396] hidden md:inline">PIPELINE: LOCAL_LLM → JEANCLAWD_VOICEBOX → PUBLISH</span>
      </div>
      <div class="flex items-center space-x-3 text-[#8b93a7]">
        <span class="text-[#00ff66] font-space">[SYS_OK]</span>
        <div class="bg-[#1f1f2a] px-2 py-0.5 border border-[#303042] text-[11px] text-[#00ff66]">EPISODE ARCHIVE</div>
      </div>
    </div>
    <div class="flex items-center justify-between px-4 bg-[#14141c] border-t border-[#1e1e28] text-[11px]">
      <div class="flex items-center space-x-1">
        <a href="/" class="px-3 py-1.5 text-[#888e9f] hover:text-[#00ff66] border-b-2 border-transparent">01:EPISODE_ARCHIVE</a>
        <span class="px-3 py-1.5 text-[#00ff66] bg-[#1e202b] border-b-2 border-[#00ff66] font-semibold">02:EPISODE // {_h(ep_tab_date)}</span>
      </div>
      <div class="text-[#596175] text-[10px] hidden sm:block">{n_nl} {nl_label} · {total_items} {story_label}</div>
    </div>
  </header>

  <!-- MAIN 2-COLUMN LAYOUT -->
  <main class="flex-grow max-w-[1780px] mx-auto p-4 lg:p-6 grid grid-cols-1 lg:grid-cols-12 gap-6 relative">

    <!-- LEFT COLUMN -->
    <div class="lg:col-span-7 flex flex-col space-y-6">

      <!-- EPISODE HEADER -->
      <section class="bg-[#17171f] border border-[#2b2b38] p-5 lg:p-6 relative overflow-hidden">
        <div class="absolute top-0 left-0 right-0 h-[2px] bg-gradient-to-r from-[#00ff66] via-[#00e5ff] to-transparent"></div>
        <div class="flex flex-wrap items-center justify-between gap-2 mb-3">
          <div class="inline-flex items-center gap-2 px-2.5 py-1 bg-[#101016] border border-[#2e2e40] text-[11px] text-[#00ff66]">
            <span class="text-white">EPISODE</span>
            <span class="text-[#3c3c50]">//</span>
            <span>PUBLISHED DIGEST</span>
          </div>
          <div class="flex items-center gap-3 text-xs text-[#8993a4]">
            <span class="flex items-center gap-1.5">
              <span class="w-2 h-2 rounded-full bg-[#00ff66]"></span>
              TTS: JEANCLAWD_VOICEBOX
            </span>
            <span class="text-[#3a3a4c]">|</span>
            <span>INGEST: RESEND MAILBOX</span>
          </div>
        </div>
        <h1 class="text-2xl sm:text-3xl font-bold font-space text-white tracking-tight leading-tight">{_h(script.title)}</h1>
        <div class="mt-2 text-sm text-[#b9ccb5] font-mono flex flex-wrap items-center gap-2 sm:gap-4">
          <span class="text-[#00ff66] font-medium">{_h(ep_date)}</span>
          <span class="text-[#3d443b]">·</span>
          <span>{n_nl} {nl_label}</span>
          <span class="text-[#3d443b]">·</span>
          <span>{total_items} {story_label}</span>
          <span class="text-[#3d443b]">·</span>
          <span class="text-[#ffb830]">Runtime: {total_dur_fmt}</span>
        </div>
      </section>

      <!-- AUDIO PLAYER -->
      <section class="bg-[#1a1a23] border border-[#2d2d3c] p-4 lg:p-5 shadow-2xl">
        <div class="flex flex-col space-y-3">
          <div class="flex items-center justify-between text-xs">
            <div class="flex items-center space-x-2">
              <span class="text-[#00ff66] font-space font-bold">[AUDIO_ENGINE:READY]</span>
              <span class="text-[#7c8396]">STEREO TTS SYNTHESIS</span>
            </div>
          </div>
          <div class="flex items-center space-x-4">
            <button id="playPauseBtn" class="flex-shrink-0 w-11 h-11 bg-[#00ff66] hover:bg-[#15ff75] text-[#0e0e13] font-bold flex items-center justify-center transition-transform active:scale-95 border border-[#4dfc94] shadow-[0_0_15px_rgba(0,255,102,0.4)]" title="Play / Pause (Space)">
              <svg id="playIcon" class="w-5 h-5 fill-current" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
              <svg id="pauseIcon" class="w-5 h-5 fill-current hidden" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
            </button>
            <div class="flex-grow flex flex-col space-y-1">
              <div class="flex justify-between text-xs font-mono">
                <div class="flex items-center space-x-2">
                  <span id="currentTime" class="text-[#00ff66] font-bold text-sm tracking-wider">00:00</span>
                  <span class="text-[#596175]">/</span>
                  <span id="totalTime" class="text-[#8e95a5]">{total_dur_fmt}</span>
                </div>
                <div id="syncLine" class="text-[11px] text-[#717182] hidden sm:block">WAITING FOR PLAYBACK</div>
              </div>
              <div class="relative w-full h-3 bg-[#101016] border border-[#2d2d3c] cursor-pointer group flex items-center" id="progressBarContainer">
                <div id="progressBar" class="absolute left-0 top-0 bottom-0 bg-[#00ff66] w-0 shadow-[0_0_8px_rgba(0,255,102,0.6)]"></div>
                <div id="progressHandle" class="absolute w-2.5 h-4 bg-white border border-[#00ff66] shadow-[0_0_6px_#00ff66] z-10 transition-transform group-hover:scale-125" style="left:calc(0% - 5px)"></div>
              </div>
            </div>
            <div class="hidden sm:flex items-center space-x-2 flex-shrink-0 pl-2 border-l border-[#2e2e3c]">
              <svg class="w-4 h-4 text-[#8993a4]" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M15.536 8.464a5 5 0 010 7.072m2.828-9.9a9 9 0 010 12.728M5.586 15H4a1 1 0 01-1-1v-4a1 1 0 011-1h1.586l4.707-4.707C10.923 3.663 12 4.109 12 5v14c0 .891-1.077 1.337-1.707.707L5.586 15z"/>
              </svg>
              <input id="volSlider" type="range" min="0" max="100" value="100" class="w-16 h-1 bg-[#262634] appearance-none cursor-pointer">
              <span id="volDisplay" class="text-[10px] text-[#717182] font-mono">100%</span>
            </div>
          </div>
          <div class="pt-2 border-t border-[#232330] flex items-center justify-between text-[11px] text-[#8993a4]">
            <div class="flex items-center space-x-3">
              <button id="skipBack" class="px-2 py-0.5 bg-[#121218] border border-[#2b2b3a] hover:border-[#00ff66] hover:text-[#00ff66] transition-colors text-[10px]">[« -15s]</button>
              <button id="skipFwd"  class="px-2 py-0.5 bg-[#121218] border border-[#2b2b3a] hover:border-[#00ff66] hover:text-[#00ff66] transition-colors text-[10px]">[+30s »]</button>
            </div>
            <div class="flex items-center space-x-2">
              <span class="text-[10px] text-[#555a6d]">FORMAT:</span>
              <span class="text-[10px] bg-[#101016] px-1.5 py-0.5 border border-[#292937] text-[#00ff66]">M4A (AAC)</span>
              <a href="{_h(audio_url)}" download class="text-[10px] text-[#8e95a5] hover:text-white underline ml-1">[DOWNLOAD]</a>
            </div>
          </div>
        </div>
      </section>

      <!-- MOBILE TABS (hidden on desktop) -->
      <div class="flex lg:hidden border-b border-[#2b2b38]" id="mobileTabs">
        <button id="tabBtnScript" onclick="switchTab('script')"
          class="flex-1 px-4 py-2.5 text-xs font-space font-bold tracking-wider border-b-2 transition-colors">
          01 // SCRIPT
        </button>
        <button id="tabBtnDigest" onclick="switchTab('digest')"
          class="flex-1 px-4 py-2.5 text-xs font-space font-bold tracking-wider border-b-2 transition-colors">
          02 // FULL DIGEST
        </button>
      </div>

      <!-- SCRIPT -->
      <section id="panelScript" class="flex flex-col space-y-3">
        <div class="flex items-center justify-between">
          <h2 class="text-sm font-bold font-space uppercase tracking-wider text-[#00ff66] flex items-center gap-2">
            <span class="text-xs bg-[#00ff66] text-black px-1 font-bold">01</span>
            Script // Narrator Feed
            <span class="text-[#656c7d] text-xs font-normal normal-case">· Click any card to seek</span>
          </h2>
          <div class="flex items-center space-x-2 text-xs text-[#717182]">
            <span class="inline-block w-2 h-2 rounded-full bg-[#00ff66]"></span>
            <span>SYNC: <strong class="text-white">ON</strong></span>
          </div>
        </div>
        <div class="space-y-3 lg:max-h-[680px] lg:overflow-y-auto pr-1" id="scriptContainer">
{script_html}
        </div>
      </section>

    </div>

    <!-- RIGHT COLUMN -->
    <div class="lg:col-span-5 flex flex-col space-y-6">

      <!-- FULL DIGEST -->
      <section id="panelDigest" class="bg-[#17171f] border border-[#2b2b38] p-5">
        <div class="flex items-center justify-between pb-3 border-b border-[#242432] mb-4">
          <div class="flex items-center space-x-2">
            <span class="text-xs bg-[#00ff66] text-black px-1 font-bold font-space">02</span>
            <h2 class="text-sm font-bold font-space uppercase tracking-wider text-white">Full Digest // Categorized</h2>
          </div>
          <span class="text-[11px] text-[#00ff66] font-mono">{total_items} Stories</span>
        </div>
        <div class="space-y-6">
{digest_html}
        </div>
      </section>

      <!-- SOURCES -->
      <section class="bg-[#17171f] border border-[#2b2b38] p-5">
        <div class="flex items-center justify-between pb-3 border-b border-[#242432] mb-3">
          <div class="flex items-center space-x-2">
            <span class="text-xs bg-[#00ff66] text-black px-1 font-bold font-space">03</span>
            <h2 class="text-sm font-bold font-space uppercase tracking-wider text-white">Ingested Sources</h2>
          </div>
          <span class="text-[11px] text-[#86efac] font-mono">{n_nl} Processed</span>
        </div>
        <div class="border border-[#262635] divide-y divide-[#222230] text-[11px] bg-[#14141c]">
{sources_html}
        </div>
      </section>

      <!-- ALL LINKS -->
      <section class="bg-[#17171f] border border-[#2b2b38] p-5">
        <div class="flex items-center justify-between pb-3 border-b border-[#242432] mb-3">
          <div class="flex items-center space-x-2">
            <span class="text-xs bg-[#00ff66] text-black px-1 font-bold font-space">04</span>
            <h2 class="text-sm font-bold font-space uppercase tracking-wider text-white">All Links // Extracted Provenance</h2>
          </div>
          <span class="text-[10px] text-[#717182] font-mono">[{len(all_citations)} URLS]</span>
        </div>
        <div class="bg-[#111116] border border-[#252534] p-3 max-h-[220px] overflow-y-auto font-mono text-[11px] space-y-1.5">
{urls_html}
        </div>
      </section>

    </div>

  </main>

  <!-- FOOTER -->
  <footer class="border-t border-[#232330] bg-[#0c0c11] py-2 px-4 text-xs font-mono text-[#717182] flex flex-wrap items-center justify-between">
    <div class="flex items-center space-x-3">
      <span class="text-[#00ff66] font-space">[STATION: HYPE_HOUND]</span>
      <span>LOCAL_INFERENCE_PIPELINE: ACTIVE</span>
    </div>
    <div class="flex items-center space-x-3 text-[11px]">
      <span>{n_nl} NEWSLETTERS INGESTED</span>
      <span class="text-[#3c3c50]">|</span>
      <span class="text-[#00ff66]">BUILT WITH HYPEHOUND + JEANCLAWD</span>
    </div>
  </footer>

<script>
{player_js}
</script>
<script>
(function () {
  var ACTIVE   = ['#00ff66', '#1e202b', '#00ff66'];  // color, bg, border
  var INACTIVE = ['#717182', 'transparent', 'transparent'];
  function applyStyle(btn, c) {
    btn.style.color = c[0]; btn.style.background = c[1]; btn.style.borderBottomColor = c[2];
  }
  window.switchTab = function (tab) {
    if (window.innerWidth >= 1024) return;
    var ps = document.getElementById('panelScript');
    var pd = document.getElementById('panelDigest');
    var bs = document.getElementById('tabBtnScript');
    var bd = document.getElementById('tabBtnDigest');
    if (tab === 'script') {
      ps.style.display = ''; pd.style.display = 'none';
      applyStyle(bs, ACTIVE); applyStyle(bd, INACTIVE);
    } else {
      ps.style.display = 'none'; pd.style.display = '';
      applyStyle(bs, INACTIVE); applyStyle(bd, ACTIVE);
    }
  };
  function init() {
    if (window.innerWidth >= 1024) return;
    var bs = document.getElementById('tabBtnScript');
    var bd = document.getElementById('tabBtnDigest');
    if (!bs) return;
    document.getElementById('panelDigest').style.display = 'none';
    applyStyle(bs, ACTIVE); applyStyle(bd, INACTIVE);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
}());
</script>
<script src="/static/ambient.js"></script>

</body>
</html>"""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def _update_episodes_index(
    stem: str,
    title: str,
    n_newsletters: int,
    n_items: int,
) -> None:
    index_path = Path("web") / "src" / "content" / "episodes.json"
    episodes: list[dict] = []
    if index_path.exists():
        import json as _json
        episodes = _json.loads(index_path.read_text())
    # Upsert by date stem
    episodes = [e for e in episodes if e.get("date") != stem]
    episodes.append({"date": stem, "title": title, "n_newsletters": n_newsletters, "n_items": n_items})
    episodes.sort(key=lambda e: e["date"], reverse=True)
    index_path.write_text(__import__("json").dumps(episodes, indent=2))


def publish_episode(
    script: PodcastScript,
    digest: Digest,
    audio_path: Path,
    period_start: datetime,
    period_end: datetime,
    out_dir: Path | None = None,
) -> Path:
    """
    Render and write the episode page.  Returns the path to index.html.
    """
    from .store import get_emails_in_period

    stem = period_end.strftime("%Y-%m-%d")
    episode_dir = out_dir or (Path("web") / "public" / "episodes" / stem)
    episode_dir.mkdir(parents=True, exist_ok=True)

    with step("Build timing manifest"):
        timing = build_timing_manifest(script, audio_path)
        total_lines = sum(len(s.lines) for s in script.segments)
        log.info(f"Timing: {len(timing)}/{total_lines} line(s) mapped across {len(script.segments)} segments")

    with step("Upload audio to GitHub release"):
        audio_url = _upload_to_github_release(audio_path, stem)

    sources = [dict(r) for r in get_emails_in_period(period_start, period_end)]

    total_items = sum(len(e.items) for e in digest.entries)

    with step("Render HTML"):
        page_html = _render_html(script, digest, timing, sources, audio_url=audio_url)
        out_html = episode_dir / "index.html"
        out_html.write_text(page_html, encoding="utf-8")

    _update_episodes_index(stem, script.title, len(sources), total_items)

    log.info(f"[green]Episode published[/green] → [bold cyan]{out_html}[/bold cyan]")
    return out_html

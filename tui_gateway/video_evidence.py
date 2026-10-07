"""Turn an uploaded video into evidence an agent can actually read (Hermex video sharing).

A model cannot play a video, so ``build_evidence`` writes a sidecar next to it::

    <name>.mp4
    <name>.mp4.evidence/
        manifest.json        status + facts (duration, size, frame times, transcript state)
        poster.jpg           one frame for client thumbnails
        contact-sheet.jpg    FRAME_COUNT evenly spaced frames in one image, read left→right, top→bottom
        frames/frame-NN.jpg  the same frames, larger, for a closer look
        transcript.txt       speech-to-text of the audio track (only when there is speech)

``video.attach`` starts the job in the background and queues the contact sheet as an image for the
next turn (``video.evidence``), so the agent sees the frames directly; the ``@file:`` reference to
the video expands into :func:`agent_note`, which mirrors the messaging gateways' inbound-video note
(``gateway/run_inbound.py``) plus the evidence paths and the transcript.

ffmpeg/ffprobe come from PATH or Hermes' managed tool store (the dashboard's PATH has neither).
Plain module: import it inside handler bodies.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

EVIDENCE_SUFFIX = ".evidence"
MANIFEST_NAME = "manifest.json"
MANIFEST_VERSION = 1
FRAME_COUNT = 12
SHEET_COLUMNS = 4
FRAME_MAX_EDGE = 720
TRANSCRIPT_INLINE_CHARS = 4000
# Below this peak level (dBFS) the audio track is treated as silent (screen recordings without mic).
SILENCE_PEAK_DB = -45.0
_PROBE_TIMEOUT = 30
_FRAME_TIMEOUT = 60
_AUDIO_TIMEOUT = 300
_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
)

TERMINAL_STATUSES = frozenset({"ready", "failed"})
TRANSCRIPT_TERMINAL = frozenset({"done", "no_audio", "silent", "unavailable", "failed"})


class EvidenceError(Exception):
    pass


def evidence_dir(video: Path) -> Path:
    return video.with_name(video.name + EVIDENCE_SUFFIX)


def is_evidence_path(path: Path) -> bool:
    return any(part.endswith(EVIDENCE_SUFFIX) for part in Path(path).parts[:-1])


# ── tools ────────────────────────────────────────────────────────────────────

def resolve_ffmpeg() -> tuple[Optional[str], Optional[str]]:
    """``(ffmpeg, ffprobe)``: PATH first, then the PM-managed ``ffmpeg`` package (ffprobe ships beside it)."""
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe
    with contextlib.suppress(Exception):
        from pm import installed_package
        package = installed_package("ffmpeg")
        if package is not None and package.binary is not None and Path(package.binary).is_file():
            binary = Path(package.binary)
            sibling = binary.with_name("ffprobe")
            ffmpeg = ffmpeg or str(binary)
            if not ffprobe and sibling.is_file():
                ffprobe = str(sibling)
    return ffmpeg, ffprobe


def _run(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, stdin=subprocess.DEVNULL, check=False)


def probe(video: Path, ffprobe: str) -> dict:
    """Duration/size facts from ffprobe; raises :class:`EvidenceError` when it isn't a readable video."""
    res = _run([ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams",
                str(video)], timeout=_PROBE_TIMEOUT)
    if res.returncode != 0:
        raise EvidenceError("not a readable video (ffprobe failed)")
    try:
        data = json.loads(res.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise EvidenceError("ffprobe returned unreadable output") from exc
    streams = data.get("streams") or []
    vstream = next((s for s in streams if s.get("codec_type") == "video"
                    and not (s.get("disposition") or {}).get("attached_pic")), None)
    if vstream is None:
        raise EvidenceError("the file has no video track")
    has_audio = any(s.get("codec_type") == "audio" for s in streams)
    duration = _float((data.get("format") or {}).get("duration")) or _float(vstream.get("duration"))
    width, height = int(vstream.get("width") or 0), int(vstream.get("height") or 0)
    rotation = _rotation(vstream)
    if rotation in (90, 270):
        width, height = height, width
    return {"duration_s": round(duration or 0.0, 3), "width": width, "height": height,
            "codec": str(vstream.get("codec_name") or ""), "has_audio": has_audio,
            "rotation": rotation}


def _float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _rotation(stream: dict) -> int:
    with contextlib.suppress(Exception):
        tag = (stream.get("tags") or {}).get("rotate")
        if tag is not None:
            return int(float(tag)) % 360
    for side in stream.get("side_data_list") or []:
        with contextlib.suppress(Exception):
            if "rotation" in side:
                return int(float(side["rotation"])) % 360
    return 0


def frame_times(duration: float, count: int = FRAME_COUNT) -> list[float]:
    """Evenly spaced sample times at bucket centres; a clip shorter than ``count`` frames' worth still
    gets ``count`` distinct-ish samples (ffmpeg returns the nearest frame)."""
    if duration <= 0:
        return [0.0]
    step = duration / count
    return [round(min(duration - 0.001, (i + 0.5) * step), 3) for i in range(count)]


def format_timestamp(seconds: float) -> str:
    seconds = max(0.0, seconds)
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:04.1f}"


def _scale_filter(width: int, height: int, max_edge: int) -> str:
    if width >= height:
        return f"scale='min({max_edge},iw)':-2"
    return f"scale=-2:'min({max_edge},ih)'"


def extract_frames(video: Path, out_dir: Path, times: list[float], ffmpeg: str, *, width: int, height: int,
                   ) -> list[dict]:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[dict] = []
    scale = _scale_filter(width, height, FRAME_MAX_EDGE)
    for index, t in enumerate(times, start=1):
        target = out_dir / f"frame-{index:02d}.jpg"
        res = _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t:.3f}", "-i", str(video),
                    "-frames:v", "1", "-vf", scale, "-q:v", "4", str(target)], timeout=_FRAME_TIMEOUT)
        if res.returncode == 0 and target.is_file() and target.stat().st_size > 0:
            frames.append({"file": f"frames/{target.name}", "t": t})
        else:
            target.unlink(missing_ok=True)
    return frames


def _font() -> Optional[str]:
    return next((f for f in _FONT_CANDIDATES if Path(f).is_file()), None)


def build_contact_sheet(evidence: Path, frames: list[dict], ffmpeg: str, *, portrait: bool) -> Optional[str]:
    """One image of every frame (``SHEET_COLUMNS`` wide), each tile stamped with its time when a font
    is available. Falls back to an unlabelled sheet rather than none."""
    if not frames:
        return None
    rows = -(-len(frames) // SHEET_COLUMNS)
    tile_w = 240 if portrait else 320
    with tempfile.TemporaryDirectory(prefix="sheet_", dir=str(evidence)) as td:
        tdir = Path(td)
        for i, frame in enumerate(frames):
            shutil.copyfile(evidence / frame["file"], tdir / f"t{i:03d}.jpg")
        target = evidence / "contact-sheet.jpg"
        base = f"scale={tile_w}:-2"
        font = _font()
        attempts = []
        if font:
            labels = ",".join(
                f"drawtext=fontfile='{font}':text='{format_timestamp(f['t']).replace(':', chr(92) + ':')}'"
                f":fontsize=18:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=4:x=6:y=6"
                f":enable='eq(n\\,{i})'"
                for i, f in enumerate(frames))
            attempts.append(f"{base},{labels},tile={SHEET_COLUMNS}x{rows}:padding=4:margin=4:color=white")
        attempts.append(f"{base},tile={SHEET_COLUMNS}x{rows}:padding=4:margin=4:color=white")
        for vf in attempts:
            res = _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-framerate", "1",
                        "-i", str(tdir / "t%03d.jpg"), "-vf", vf, "-frames:v", "1", "-q:v", "3",
                        str(target)], timeout=_FRAME_TIMEOUT)
            if res.returncode == 0 and target.is_file() and target.stat().st_size > 0:
                return target.name
    return None


def extract_audio(video: Path, target: Path, ffmpeg: str) -> bool:
    res = _run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-vn", "-ac", "1",
                "-ar", "16000", "-c:a", "aac", "-b:a", "32k", "-movflags", "+faststart", str(target)],
               timeout=_AUDIO_TIMEOUT)
    return res.returncode == 0 and target.is_file() and target.stat().st_size > 0


def peak_volume_db(audio: Path, ffmpeg: str) -> Optional[float]:
    res = _run([ffmpeg, "-hide_banner", "-nostats", "-i", str(audio), "-af", "volumedetect", "-f", "null",
                "-"], timeout=_AUDIO_TIMEOUT)
    for line in (res.stderr or "").splitlines():
        if "max_volume:" in line:
            with contextlib.suppress(ValueError, IndexError):
                return float(line.split("max_volume:")[1].split("dB")[0].strip())
    return None


def default_transcriber(audio_path: str) -> dict:
    from tools.transcription_tools import transcribe_audio
    return transcribe_audio(audio_path, source="video_evidence")


# ── manifest ─────────────────────────────────────────────────────────────────

def read_manifest(video: Path) -> Optional[dict]:
    path = evidence_dir(video) / MANIFEST_NAME
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) and data.get("version") == MANIFEST_VERSION else None


def _write_manifest(evidence: Path, manifest: dict) -> None:
    manifest["updated_at"] = time.time()
    tmp = evidence / f".{MANIFEST_NAME}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2, sort_keys=True)
    os.replace(tmp, evidence / MANIFEST_NAME)


def initial_manifest(video: Path, facts: dict, *, display_name: str) -> dict:
    return {
        "version": MANIFEST_VERSION, "status": "pending", "display_name": display_name,
        "video": {"name": video.name, "bytes": video.stat().st_size, **facts},
        "poster": None, "contact_sheet": None,
        "contact_sheet_layout": None, "frames": [],
        "transcript": {"status": "pending", "file": None, "chars": 0},
        "errors": [],
    }


def write_initial_manifest(video: Path, facts: dict, *, display_name: str) -> dict:
    evidence = evidence_dir(video)
    evidence.mkdir(parents=True, exist_ok=True)
    manifest = initial_manifest(video, facts, display_name=display_name)
    _write_manifest(evidence, manifest)
    return manifest


def build_evidence(video: Path, *, ffmpeg: Optional[str], transcriber: Callable[[str], dict] = default_transcriber,
                   frame_count: int = FRAME_COUNT) -> dict:
    """Fill the sidecar in stages, rewriting ``manifest.json`` after each so a reader mid-way sees
    honest progress. Never raises for a media problem: failures land in ``manifest["errors"]``."""
    evidence = evidence_dir(video)
    evidence.mkdir(parents=True, exist_ok=True)
    manifest = read_manifest(video)
    if manifest is None:
        raise EvidenceError("call write_initial_manifest first")
    facts = manifest["video"]
    manifest["status"] = "processing"
    _write_manifest(evidence, manifest)
    if not ffmpeg:
        manifest["errors"].append("ffmpeg is not installed on the Hermes host")
        manifest["transcript"] = {"status": "unavailable", "file": None, "chars": 0}
        manifest["status"] = "failed"
        _write_manifest(evidence, manifest)
        return manifest

    width, height = int(facts.get("width") or 0), int(facts.get("height") or 0)
    try:
        frames = extract_frames(video, evidence / "frames", frame_times(float(facts.get("duration_s") or 0),
                                frame_count), ffmpeg, width=width, height=height)
    except (OSError, subprocess.TimeoutExpired) as exc:
        frames = []
        manifest["errors"].append(f"frame extraction failed: {type(exc).__name__}")
    manifest["frames"] = frames
    if frames:
        middle = frames[len(frames) // 2]
        with contextlib.suppress(OSError):
            shutil.copyfile(evidence / middle["file"], evidence / "poster.jpg")
            manifest["poster"] = "poster.jpg"
        try:
            sheet = build_contact_sheet(evidence, frames, ffmpeg, portrait=height > width)
        except (OSError, subprocess.TimeoutExpired) as exc:
            sheet = None
            manifest["errors"].append(f"contact sheet failed: {type(exc).__name__}")
        manifest["contact_sheet"] = sheet
        if sheet:
            manifest["contact_sheet_layout"] = {
                "columns": SHEET_COLUMNS, "rows": -(-len(frames) // SHEET_COLUMNS), "order": "left-to-right, top-to-bottom"}
    else:
        manifest["errors"].append("no frames could be extracted")
    _write_manifest(evidence, manifest)

    manifest["transcript"] = _transcribe(video, evidence, ffmpeg, has_audio=bool(facts.get("has_audio")),
                                         transcriber=transcriber, errors=manifest["errors"])
    manifest["status"] = "ready" if frames else "failed"
    _write_manifest(evidence, manifest)
    return manifest


def _transcribe(video: Path, evidence: Path, ffmpeg: str, *, has_audio: bool,
                transcriber: Callable[[str], dict], errors: list) -> dict:
    if not has_audio:
        return {"status": "no_audio", "file": None, "chars": 0}
    with tempfile.TemporaryDirectory(prefix="audio_", dir=str(evidence)) as td:
        audio = Path(td) / "audio.m4a"
        try:
            if not extract_audio(video, audio, ffmpeg):
                errors.append("audio extraction failed")
                return {"status": "failed", "file": None, "chars": 0}
            peak = peak_volume_db(audio, ffmpeg)
        except subprocess.TimeoutExpired:
            errors.append("audio extraction timed out")
            return {"status": "failed", "file": None, "chars": 0}
        if peak is not None and peak < SILENCE_PEAK_DB:
            return {"status": "silent", "file": None, "chars": 0, "peak_db": peak}
        try:
            result = transcriber(str(audio)) or {}
        except Exception as exc:  # a provider bug must not lose the visual evidence
            errors.append(f"transcription raised {type(exc).__name__}")
            return {"status": "failed", "file": None, "chars": 0}
    if not result.get("success"):
        reason = str(result.get("error") or "transcription failed")[:200]
        status = "unavailable" if "provider" in reason.lower() or "no stt" in reason.lower() else "failed"
        return {"status": status, "file": None, "chars": 0, "error": reason}
    text = str(result.get("transcript") or "").strip()
    if not text:
        return {"status": "silent", "file": None, "chars": 0}
    (evidence / "transcript.txt").write_text(text + "\n", encoding="utf-8")
    return {"status": "done", "file": "transcript.txt", "chars": len(text),
            "provider": str(result.get("provider") or "")}


# ── background jobs ──────────────────────────────────────────────────────────

class EvidenceJobs:
    """At most one job per video path; the runner is injectable for tests."""

    def __init__(self, runner: Optional[Callable[[Path], Any]] = None):
        self._running: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
        self._runner = runner

    def start(self, video: Path, *, scope_factory: Optional[Callable[[], Any]] = None) -> bool:
        """``scope_factory`` returns a context manager entered ON the job thread (profile ContextVars
        don't cross threads), so transcription reads the session profile's STT config."""
        key = str(video)
        with self._lock:
            thread = self._running.get(key)
            if thread is not None and thread.is_alive():
                return False
            thread = threading.Thread(target=self._run, args=(video, scope_factory),
                                      name=f"video-evidence-{video.name[:24]}", daemon=True)
            self._running[key] = thread
        thread.start()
        return True

    def is_running(self, video: Path) -> bool:
        with self._lock:
            thread = self._running.get(str(video))
        return bool(thread and thread.is_alive())

    def wait(self, video: Path, timeout: float | None = None) -> None:
        with self._lock:
            thread = self._running.get(str(video))
        if thread is not None:
            thread.join(timeout)

    def _run(self, video: Path, scope_factory: Optional[Callable[[], Any]]) -> None:
        try:
            if self._runner is not None:
                self._runner(video)
                return
            ffmpeg, _ = resolve_ffmpeg()
            with (scope_factory() if scope_factory is not None else contextlib.nullcontext()):
                build_evidence(video, ffmpeg=ffmpeg)
        except Exception:
            logger.exception("video evidence job failed")
            with contextlib.suppress(Exception):
                manifest = read_manifest(video)
                if manifest is not None and manifest.get("status") not in TERMINAL_STATUSES:
                    manifest["status"] = "failed"
                    manifest.setdefault("errors", []).append("evidence job crashed")
                    if manifest.get("transcript", {}).get("status") not in TRANSCRIPT_TERMINAL:
                        manifest["transcript"] = {"status": "failed", "file": None, "chars": 0}
                    _write_manifest(evidence_dir(video), manifest)
        finally:
            with self._lock:
                self._running.pop(str(video), None)


_jobs: Optional[EvidenceJobs] = None
_jobs_lock = threading.Lock()


def jobs() -> EvidenceJobs:
    global _jobs
    with _jobs_lock:
        if _jobs is None:
            _jobs = EvidenceJobs()
        return _jobs


def set_jobs_for_tests(value: Optional[EvidenceJobs]) -> None:
    global _jobs
    with _jobs_lock:
        _jobs = value


# ── what the agent reads ─────────────────────────────────────────────────────

def evidence_summary(video: Path, manifest: Optional[dict]) -> dict:
    """Client-facing status (no transcript text): paths are absolute so the app can fetch the poster."""
    evidence = evidence_dir(video)
    if manifest is None:
        return {"status": "missing", "poster_path": None, "contact_sheet_path": None, "frame_count": 0,
                "transcript_status": "pending", "errors": []}
    def _abs(name):
        return str(evidence / name) if name else None
    return {"status": manifest.get("status") or "pending", "poster_path": _abs(manifest.get("poster")),
            "contact_sheet_path": _abs(manifest.get("contact_sheet")),
            "frame_count": len(manifest.get("frames") or []),
            "transcript_status": (manifest.get("transcript") or {}).get("status") or "pending",
            "errors": list(manifest.get("errors") or [])[:5]}


def agent_note(video: Path, manifest: Optional[dict], *, display_name: str = "",
               visible: Callable[[str], str] = lambda p: p) -> str:
    """The text an ``@file:`` reference to an uploaded video expands into. Mirrors the inbound-video
    note (path, "inspect it yourself", don't ask the user to describe it) and adds the evidence."""
    name = display_name or (manifest or {}).get("display_name") or video.name
    lines = [
        f"[The user sent a video attachment: '{name}'. It is saved at: {visible(str(video))}. "
        "Its frames and audio are not inlined as video; this note lists evidence prepared from it. "
        "If the user's request involves what the video shows, inspect the evidence yourself "
        "instead of asking the user to describe it.]"]
    if manifest is None:
        lines.append("Evidence: not prepared. Use a video analysis or media tool on the path above "
                     "(for example ffmpeg to sample frames, then view them).")
        return "\n".join(lines)
    facts = manifest.get("video") or {}
    duration = float(facts.get("duration_s") or 0)
    size_mb = (facts.get("bytes") or 0) / (1024 * 1024)
    lines.append(f"Video: {format_timestamp(duration)} long, {facts.get('width')}x{facts.get('height')}, "
                 f"{size_mb:.1f} MB, audio track: {'yes' if facts.get('has_audio') else 'no'}.")
    evidence = evidence_dir(video)
    status = manifest.get("status")
    if status not in TERMINAL_STATUSES:
        lines.append(f"Evidence is still being prepared in {visible(str(evidence))} "
                     f"(read {MANIFEST_NAME} there for progress).")
    frames = manifest.get("frames") or []
    if manifest.get("contact_sheet"):
        layout = manifest.get("contact_sheet_layout") or {}
        times = ", ".join(format_timestamp(f["t"]) for f in frames)
        lines.append(
            f"Contact sheet (also attached to this message as an image when the app sent it): "
            f"{visible(str(evidence / manifest['contact_sheet']))}, {len(frames)} frames in "
            f"{layout.get('columns')} columns, read left to right, top to bottom, at {times}.")
    if frames:
        lines.append(f"Larger frames: {visible(str(evidence / 'frames'))} (frame-01.jpg … "
                     f"frame-{len(frames):02d}.jpg, same times). View them with your vision tool for detail; "
                     "sample more frames with ffmpeg from the video path if you need finer timing.")
    transcript = manifest.get("transcript") or {}
    tstatus = transcript.get("status")
    if tstatus == "done" and transcript.get("file"):
        path = evidence / transcript["file"]
        text = ""
        with contextlib.suppress(OSError):
            text = path.read_text(encoding="utf-8").strip()
        if len(text) > TRANSCRIPT_INLINE_CHARS:
            text = text[:TRANSCRIPT_INLINE_CHARS].rstrip() + f" … (truncated; full text in {visible(str(path))})"
        lines.append(f"Audio transcript ({visible(str(path))}):\n{text}")
    elif tstatus == "no_audio":
        lines.append("Audio: the video has no audio track.")
    elif tstatus == "silent":
        lines.append("Audio: no speech detected.")
    elif tstatus == "unavailable":
        lines.append("Audio: not transcribed (no speech-to-text provider is available on this host).")
    elif tstatus == "failed":
        lines.append("Audio: transcription failed; transcribe the video's audio yourself if it matters.")
    else:
        lines.append(f"Audio: transcript still in progress (it will be written to "
                     f"{visible(str(evidence / 'transcript.txt'))}).")
    if status == "failed" and not frames:
        lines.append("Frames could not be extracted; use a media tool on the video path directly.")
    return "\n".join(lines)

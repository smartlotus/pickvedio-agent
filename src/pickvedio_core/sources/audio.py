"""Media channel: download the smallest usable audio, normalise it, transcribe it.

Ported from the desktop app's ``BilibiliDownloader`` + ``AudioProcessor`` with
the container-specific parts handled:

* FFmpeg is resolved from ``PVC_FFMPEG`` or ``PATH`` only. The app's candidate
  list included a hard-coded third-party ``node_modules`` path on one Windows
  machine, which is meaningless -- and misleading -- in a deployed image.
* 16 kHz mono WAV is exactly 32 000 bytes per second, so a 3-hour recording is
  346 MB: far past every hosted ASR upload ceiling. The audio is therefore cut
  into ``PVC_ASR_CHUNK_SECONDS`` blocks *after* normalisation, each transcribed
  separately with the block offset added back. Timestamps stay continuous.
* Downloads use the app's "worst quality wins" format selection; a summary needs
  speech, not pixels.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
import threading
import time
import wave
from pathlib import Path
from typing import Any
from collections.abc import Callable

from ..errors import DependencyError, PickvedioError, TransientNetworkError
from ..models import Segment, SourceKind, Transcript
from ..util import format_time

logger = logging.getLogger("pickvedio.audio")

BYTES_PER_SECOND_16K_MONO_PCM16 = 32_000
MAX_FFMPEG_STDERR_KEPT = 16_000


def find_ffmpeg(configured: str = "") -> str:
    candidates = [configured, str(Path(shutil.which("ffmpeg") or ""))]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return str(candidate)
    raise DependencyError(
        "未找到 ffmpeg。音频转写通道需要它：Debian/Ubuntu `apt-get install -y ffmpeg`，"
        "Windows 安装后把路径写入 PVC_FFMPEG。字幕通道不需要 ffmpeg。"
    )


def wav_duration(path: Path) -> float | None:
    try:
        with wave.open(str(path), "rb") as handle:
            frames, rate = handle.getnframes(), handle.getframerate()
        return frames / rate if rate else None
    except (OSError, wave.Error):
        size = path.stat().st_size if path.is_file() else 0
        if size > 44:
            return (size - 44) / BYTES_PER_SECOND_16K_MONO_PCM16
        return None


def slice_wav(source: Path, destination: Path, start_seconds: float, end_seconds: float) -> Path:
    with wave.open(str(source), "rb") as handle:
        params = handle.getparams()
        rate = handle.getframerate()
        handle.setpos(int(start_seconds * rate))
        frames = handle.readframes(int((end_seconds - start_seconds) * rate))
    with wave.open(str(destination), "wb") as target:
        target.setparams(params)
        target.writeframes(frames)
    return destination


def slice_audio(source: Path, directory: Path, chunk_seconds: int) -> list[tuple[Path, float]]:
    """Split normalised audio into upload-sized blocks with their offsets."""
    directory.mkdir(parents=True, exist_ok=True)
    total = wav_duration(source)
    if not total or total <= chunk_seconds:
        return [(source, 0.0)]
    blocks: list[tuple[Path, float]] = []
    index = 0
    start = 0.0
    while start < total:
        end = min(start + chunk_seconds, total)
        path = directory / f"chunk_{index:03d}.wav"
        blocks.append((slice_wav(source, path, start, end), start))
        start = end
        index += 1
    return blocks


class AudioPipeline:
    def __init__(
        self,
        *,
        ffmpeg_path: str = "",
        cookie_file: str = "",
        sessdata: str = "",
        only_audio: bool = True,
        chunk_seconds: int = 600,
    ) -> None:
        self.ffmpeg_path = ffmpeg_path
        self.cookie_file = cookie_file
        self.sessdata = sessdata
        self.only_audio = only_audio
        self.chunk_seconds = max(60, int(chunk_seconds))

    # -- download ------------------------------------------------------------

    def download(self, url: str, output_dir: Path,
                 on_progress: Callable[[float, str], None] | None = None,
                 cancel: Callable[[], None] = lambda: None) -> Path:
        try:
            import yt_dlp  # type: ignore
        except ImportError as exc:
            raise DependencyError(
                "未安装 yt-dlp，无法下载音频。安装：pip install '.[media]'。"
            ) from exc
        output_dir.mkdir(parents=True, exist_ok=True)
        options: dict[str, Any] = {
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            # Bilibili's audio CDN can close a long single request before the
            # announced Content-Length; chunked Range requests plus a resume
            # budget beat restarting a 50 MB stream from byte zero.
            "http_chunk_size": 5 * 1024 * 1024,
            "continuedl": True,
            "retries": 10,
            "fragment_retries": 10,
            "file_access_retries": 3,
            "socket_timeout": 90,
            "outtmpl": str(output_dir / "source.%(ext)s"),
            "format": "worstaudio/worst" if self.only_audio else "worstvideo*+worstaudio/worst",
            "merge_output_format": "mp4",
            "logger": logging.getLogger("pickvedio.yt-dlp"),
        }
        if self.cookie_file:
            options["cookiefile"] = self.cookie_file
        if self.ffmpeg_path and Path(self.ffmpeg_path).is_file():
            options["ffmpeg_location"] = str(Path(self.ffmpeg_path).parent)

        def hook(data: dict[str, Any]) -> None:
            cancel()
            if on_progress is None:
                return
            if data.get("status") == "downloading":
                total = data.get("total_bytes") or data.get("total_bytes_estimate") or 0
                done = data.get("downloaded_bytes") or 0
                speed = data.get("speed") or 0
                on_progress((done / total) if total else 0.0, f"下载中 · {speed / 1024:.0f} KB/s")
            elif data.get("status") == "finished":
                on_progress(1.0, "下载完成，正在准备文件")

        options["progress_hooks"] = [hook]
        last: Exception | None = None
        for attempt, delay in enumerate((0.0, 2.0, 8.0)):
            if delay:
                time.sleep(delay)
            try:
                with yt_dlp.YoutubeDL(options) as ydl:  # type: ignore[attr-defined]
                    info = ydl.extract_info(url, download=True) or {}
                    name = ydl.prepare_filename(info)
                path = Path(name)
                if not path.is_file():
                    matches = sorted(
                        output_dir.glob("source.*"), key=lambda item: item.stat().st_mtime, reverse=True
                    )
                    if not matches:
                        raise PickvedioError("下载器没有返回媒体文件，请检查登录状态与链接可访问性。")
                    path = matches[0]
                return path
            except Exception as exc:  # yt_dlp.DownloadError
                cancel()
                last = exc
                text = str(exc).lower()
                if "sign in to confirm" in text or "not a bot" in text:
                    from ..errors import UpstreamBlocked

                    raise UpstreamBlocked(
                        "站点拒绝了自动化下载会话。", platform="generic",
                        hint="改走浏览器回传通道，或提供 PVC_COOKIE_FILE。",
                    ) from exc
                if attempt == 0 and "ffmpeg" not in text and "requested format is not available" not in text:
                    continue
                if "timed out" in text or "ssl" in text or "reset" in text:
                    continue
                break
        raise TransientNetworkError(f"音频下载失败：{str(last)[:300]}") from last

    # -- normalise -----------------------------------------------------------

    def normalize(self, source: Path, destination: Path,
                  on_progress: Callable[[float, str], None] | None = None,
                  duration_seconds: float | None = None,
                  cancel: Callable[[], None] = lambda: None) -> Path:
        if not source.is_file() or source.stat().st_size <= 0:
            raise PickvedioError(f"音频源文件不存在或为空：{source}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        command = [
            find_ffmpeg(self.ffmpeg_path), "-y", "-hide_banner", "-nostdin", "-v", "error",
            "-progress", "pipe:2", "-i", str(source),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(destination),
        ]
        popen_kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.PIPE,
        }
        if threading.current_thread() is threading.main_thread():
            pass  # reserved: signal handling is installed by the caller, not here
        process = subprocess.Popen(command, **popen_kwargs)  # noqa: S603 - fixed argv, no shell
        stderr_tail = bytearray()

        def drain() -> None:
            pending = b""
            stream = process.stderr
            if stream is None:
                return
            while True:
                chunk = stream.read(8192)
                if not chunk:
                    break
                stderr_tail.extend(chunk)
                if len(stderr_tail) > MAX_FFMPEG_STDERR_KEPT:
                    del stderr_tail[:-MAX_FFMPEG_STDERR_KEPT]
                if on_progress is None:
                    continue
                pending += chunk
                lines = pending.split(b"\n")
                pending = lines.pop()
                for raw in lines:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if line.startswith(("out_time_us=", "out_time_ms=")) and duration_seconds:
                        try:
                            current = int(line.split("=", 1)[1]) / 1_000_000
                        except ValueError:
                            continue
                        on_progress(
                            max(0.0, min(0.99, current / duration_seconds)),
                            f"音频预处理 · {format_time(current)} / {format_time(duration_seconds)}",
                        )
                    elif line == "progress=end":
                        on_progress(1.0, "音频预处理完成")

        reader = threading.Thread(target=drain, name="pvc-ffmpeg", daemon=True)
        reader.start()
        started = time.monotonic()
        max_runtime = min(8 * 3600, max(15 * 60, int((duration_seconds or 0) * 1.5 + 600)))
        timed_out = False
        while process.poll() is None:
            cancel()
            if time.monotonic() - started > max_runtime:
                timed_out = True
                process.terminate()
                break
            time.sleep(0.25)
        if process.poll() is None:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        reader.join(timeout=3)
        detail = bytes(stderr_tail).decode("utf-8", errors="replace").strip()
        if timed_out:
            raise PickvedioError(f"FFmpeg 音频预处理超过 {max_runtime // 60} 分钟，已停止。{detail[-800:]}")
        code = process.returncode
        if code is not None and code >= 2**31:  # Windows reports negatives as unsigned 32-bit
            code -= 2**32
        if code != 0 or not destination.is_file() or destination.stat().st_size <= 44:
            raise PickvedioError(
                f"FFmpeg 音频处理失败（退出码 {code}）：{detail[-1200:] or 'FFmpeg 没有返回诊断信息。'}"
            )
        return destination

    # -- transcribe ----------------------------------------------------------

    def run(self, url: str, workdir: Path, transcriber,
            on_progress: Callable[[float, str], None] | None = None,
            cancel: Callable[[], None] = lambda: None,
            duration_hint: float | None = None,
            title: str = "") -> Transcript:
        workdir = Path(workdir)
        media = self.download(url, workdir / "source", on_progress, cancel)
        audio = self.normalize(
            media, workdir / "audio" / "audio_16k_mono.wav", on_progress, duration_hint, cancel
        )
        blocks_dir = workdir / "audio" / "blocks"
        blocks = slice_audio(audio, blocks_dir, self.chunk_seconds)
        total = wav_duration(audio) or 0.0
        segments: list[Segment] = []
        try:
            for index, (block, offset) in enumerate(blocks, start=1):
                cancel()
                local = transcriber.transcribe(
                    block, _block_reporter(on_progress, index, len(blocks), block, offset, total), cancel
                )
                segments.extend(
                    Segment(start=item.start + offset, end=item.end + offset, text=item.text,
                            speaker=item.speaker)
                    for item in local
                )
        finally:
            for block, _ in blocks:
                if block.parent == blocks_dir:
                    block.unlink(missing_ok=True)
        if not segments:
            raise PickvedioError("转写结果为空：可能是纯音乐、静音，或语言设置不正确。")
        return Transcript(
            video_url=url,
            title=title or "未命名视频",
            duration=total or None,
            segments=sorted(segments, key=lambda item: item.start),
            source=SourceKind.WHISPER,
            language=getattr(getattr(transcriber, "settings", None), "language", "") or "",
            channel=transcriber.__class__.__name__,
            meta={"audio_bytes": audio.stat().st_size, "blocks": len(blocks)},
        )


def _block_seconds(block: Path, total: float) -> float:
    duration = wav_duration(block)
    return duration if duration else 0.0


def _block_reporter(on_progress, index: int, count: int, block: Path, offset: float, total: float):
    """Map one block's 0-1 progress onto the whole recording.

    A factory rather than an inline lambda because the closure must bind *this*
    block: a late-binding lambda reports every block's progress against the
    last file in the loop.
    """
    if on_progress is None:
        return None
    block_seconds = _block_seconds(block, total)

    def report(ratio: float, message: str) -> None:
        done = offset + ratio * block_seconds
        on_progress(
            min(0.99, done / total) if total else ratio,
            f"块 {index}/{count} · {message}",
        )

    return report

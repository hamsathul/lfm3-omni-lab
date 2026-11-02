"""LFM3-Omni Lab: one multimodal conversation with a local LFM3-Omni model.

The model runs in app/worker.py as a child process. Start, Stop and Restart
spawn and kill that process; jobs go to it over stdin and results stream back.
This file serves the page, converts attachments, and runs the tool-calling loop.
"""

import asyncio
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
import wave
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import numpy as np
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import UploadFile

from app import browser, tools
from app.guards import CONTEXT_TOKENS

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
MEDIA = DATA / "media"
OUTPUTS = DATA / "outputs"
MODEL_ID = os.environ.get("LFM3_MODEL", "LiquidAI/LFM3-Omni-2B-MLX-8bit")
VIDEO_MAX_SECONDS = 30
VIDEO_MAX_SIDE = 640

# From the LFM3-Omni-2B model card
SPEECH_LANGUAGES = [
    "Arabic", "Chinese", "Danish", "Dutch", "Finnish", "French", "German", "Greek",
    "Hebrew", "Italian", "Japanese", "Korean", "Polish", "Portuguese", "Spanish",
    "Swedish", "Turkish",
]
TTS_LANGUAGES = ["American English", "British English"] + SPEECH_LANGUAGES
VOICES = ["female", "male"]



def find_tool(name):
    found = shutil.which(name)
    if found:
        return found
    for folder in ("/opt/homebrew/bin", "/usr/local/bin"):
        candidate = Path(folder) / name
        if candidate.exists():
            return str(candidate)
    return None


FFMPEG = find_tool("ffmpeg")
FFPROBE = find_tool("ffprobe")
ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class MediaError(Exception):
    pass


class ModelProcess:
    """Owns the worker process and everything the page shows about it."""

    def __init__(self):
        self.proc = None
        self.state = "stopped"  # stopped | starting | ready | stopping | error
        self.error = None
        self.started_at = None
        self.ready_at = None
        self.load_seconds = None
        self.memory_gb = None
        self.busy = False
        self.current_job = None
        self.events = None
        self.logs = deque(maxlen=800)
        self.log_seq = 0
        self.job_lock = asyncio.Lock()
        self.control_lock = asyncio.Lock()
        self.supervisor = None

    # ---- log -------------------------------------------------------------
    def log(self, line, source="app"):
        line = ANSI.sub("", line).rstrip()
        if not line.strip():
            return
        # Progress bars redraw the same line; keep only the latest version.
        if "%|" in line and self.logs and "%|" in self.logs[-1]["line"]:
            if self.logs[-1]["line"].split(":")[0] == line.split(":")[0]:
                self.logs.pop()
        self.log_seq += 1
        self.logs.append({"n": self.log_seq, "t": time.time(), "src": source, "line": line})

    # ---- status ----------------------------------------------------------
    def alive(self):
        return self.proc is not None and self.proc.returncode is None

    def status(self):
        pid = self.proc.pid if self.alive() else None
        return {
            "state": self.state,
            "busy": self.busy,
            "model": MODEL_ID,
            "pid": pid,
            "uptime_seconds": round(time.time() - self.ready_at) if self.state == "ready" and self.ready_at else None,
            "starting_seconds": round(time.time() - self.started_at) if self.state == "starting" and self.started_at else None,
            "load_seconds": self.load_seconds,
            "memory_gb": self.memory_gb,
            "process_memory_gb": process_memory_gb(pid) if pid else None,
            "error": self.error,
            "ffmpeg": bool(FFMPEG),
        }

    # ---- lifecycle -------------------------------------------------------
    async def start(self):
        async with self.control_lock:
            await self._start()

    async def stop(self):
        async with self.control_lock:
            await self._stop()

    async def restart(self):
        async with self.control_lock:
            self.log("Restarting the model")
            await self._stop()
            await self._start()

    async def _start(self):
        if self.alive():
            return
        self.state, self.error = "starting", None
        self.started_at, self.ready_at = time.time(), None
        self.load_seconds = self.memory_gb = None
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "TOKENIZERS_PARALLELISM": "false",
               # Killing the process makes Python warn about a leaked semaphore; it's harmless noise.
               "PYTHONWARNINGS": "ignore:resource_tracker:UserWarning"}
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, "-u", str(ROOT / "app" / "worker.py"),
            "--model", MODEL_ID, "--outputs", str(OUTPUTS),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # its own process group, so Stop kills everything it spawned
            limit=64 * 1024 * 1024,
            env=env,
        )
        self.log(f"Started the model process (pid {self.proc.pid}) for {MODEL_ID}")
        self.supervisor = asyncio.create_task(self._supervise(self.proc))

    async def _stop(self):
        proc = self.proc
        if proc is None or proc.returncode is not None:
            self.state = "stopped" if self.state != "error" else self.state
            return
        self.state = "stopping"
        self.log(f"Stopping the model process (pid {proc.pid})")
        _signal_group(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            self.log("The model didn't stop within 10 s; forcing it")
            _signal_group(proc.pid, signal.SIGKILL)
            await proc.wait()
        if self.supervisor:
            try:
                await asyncio.wait_for(self.supervisor, timeout=5)
            except asyncio.TimeoutError:
                pass

    async def _supervise(self, proc):
        stderr_task = asyncio.create_task(self._read_stderr(proc))
        await self._read_stdout(proc)
        code = await proc.wait()
        try:
            await asyncio.wait_for(stderr_task, timeout=2)
        except asyncio.TimeoutError:
            stderr_task.cancel()
        if proc is not self.proc:
            return
        if self.events is not None and self.current_job:
            self.events.put_nowait({
                "event": "error", "id": self.current_job,
                "message": "The model was stopped before it finished this request.",
            })
        if self.state == "stopping":
            self.state = "stopped"
            self.log("Model stopped. Its memory has been released.")
        else:
            self.state = "error"
            self.error = self.error or f"The model process exited unexpectedly (exit code {code}). Check the log, then start it again."
            self.log(self.error)
        self.proc, self.ready_at, self.memory_gb, self.load_seconds = None, None, None, None

    async def _read_stdout(self, proc):
        while True:
            try:
                raw = await proc.stdout.readline()
            except (ValueError, asyncio.LimitOverrunError):
                continue
            if not raw:
                return
            try:
                event = json.loads(raw)
            except json.JSONDecodeError:
                self.log(raw.decode(errors="replace"), "model")
                continue
            kind = event.get("event")
            if "memory_gb" in event:
                self.memory_gb = event["memory_gb"]
            if kind == "loading":
                self.log("Loading the model weights")
            elif kind == "ready":
                self.state, self.ready_at = "ready", time.time()
                self.load_seconds = event.get("load_seconds")
                self.log(f"Model ready, loaded in {self.load_seconds} s")
            elif kind == "fatal":
                self.error = event.get("message")
                self.log(self.error)
            elif self.events is not None and event.get("id") == self.current_job:
                self.events.put_nowait(event)

    async def _read_stderr(self, proc):
        pending = ""
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            pending += chunk.decode(errors="replace")
            *lines, pending = re.split(r"[\r\n]", pending)
            for line in lines:
                self.log(line, "model")
        if pending:
            self.log(pending, "model")

    # ---- jobs ------------------------------------------------------------
    async def run(self, job):
        async with self.job_lock:
            if self.state != "ready" or not self.alive():
                yield {"event": "error", "id": job["id"], "message": "The model isn't running. Start it, then try again."}
                return
            self.events, self.current_job, self.busy = asyncio.Queue(), job["id"], True
            finished = False
            try:
                self.proc.stdin.write((json.dumps(job) + "\n").encode())
                await self.proc.stdin.drain()
                while True:
                    event = await self.events.get()
                    if event.get("event") == "done":
                        self.log(job_summary(job, event.get("stats") or {}))
                    elif event.get("event") == "error" and event.get("code") != "context":
                        self.log(f"{job.get('output', 'text')} reply failed: {event.get('message')}")
                    yield event
                    if event.get("event") in ("done", "error"):
                        finished = True
                        break
            finally:
                if not finished:  # the page went away mid-answer
                    self.cancel(job["id"])
                self.events, self.current_job, self.busy = None, None, False

    def cancel(self, job_id):
        if self.alive():
            try:
                self.proc.stdin.write((json.dumps({"cmd": "cancel", "id": job_id}) + "\n").encode())
            except (BrokenPipeError, ConnectionResetError):
                pass


FINISH_WORDS = {"stop": "finished", "length": "hit the length limit", "cancelled": "stopped",
                "noise": "speech cut where it turned to noise", "overrun": "speech cut, it ran past the text",
                "wrote": "started a written answer, so it was asked again to speak"}


def job_summary(job, stats):
    """One log line for a finished job: what it was, its size and its speed."""
    kind = {"text": "Written", "audio": "Read-aloud", "interleaved": "Spoken", "auto": "Spoken"}.get(job.get("output"), "Reply")
    parts = [f"{kind} reply: {stats.get('prompt_tokens', 0):,} prompt tokens of {stats.get('context_tokens', 32768) // 1024}K"]
    parts.append(f"{stats.get('tokens', 0)} generated in {stats.get('seconds', 0)} s ({stats.get('tokens_per_sec', 0)}/s)")
    if stats.get("audio_seconds"):
        parts.append(f"{stats['audio_seconds']} s of speech")
    if stats.get("first_audio_seconds"):
        parts.append(f"first sound after {stats['first_audio_seconds']} s")
    parts.append(f"peak {stats.get('peak_memory_gb', 0)} GB")
    parts.append(FINISH_WORDS.get(stats.get("finish"), str(stats.get("finish"))))
    return ", ".join(parts)


def _signal_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


def process_memory_gb(pid):
    try:
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=2).stdout
        return round(int(out.strip()) * 1024 / 1e9, 2)
    except (ValueError, subprocess.SubprocessError, OSError):
        return None


# ---- media preparation ---------------------------------------------------
def ffmpeg(*args):
    if not FFMPEG:
        raise MediaError("ffmpeg isn't installed. Install it with: brew install ffmpeg")
    result = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", *args],
                            capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1] if result.stderr.strip() else "unknown error"
        raise MediaError(f"Couldn't read that file ({detail}).")


def to_wav(src, dest, max_seconds=None):
    limit = ["-t", str(max_seconds)] if max_seconds else []
    ffmpeg("-i", str(src), *limit, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(dest))
    soften(dest)
    return str(dest)


def soften(path, ceiling=0.8, target=0.7):
    """Turn down audio that peaks near full scale.

    A live-mode recording boosted by the browser's automatic gain (RMS 0.23, peaks at full
    scale) was "transcribed" as an answer: "What time is it in Tokyo right now?" came back as
    "I don't know the current time in Tokyo." The same recording at 70% volume or lower came
    back word for word, so loud audio is scaled to peak at 70%. Quiet audio is left alone.
    """
    with wave.open(str(path), "rb") as w:
        params = w.getparams()
        samples = np.frombuffer(w.readframes(params.nframes), dtype=np.int16)
    peak = int(np.abs(samples.astype(np.int32)).max()) if samples.size else 0
    if peak <= ceiling * 32767:
        return
    scaled = np.round(samples.astype(np.float32) * (target * 32767 / peak)).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setparams(params)
        w.writeframes(scaled.tobytes())


def has_stream(src, kind):
    """kind is "a" (audio) or "V" (real video, not cover art)."""
    if not FFPROBE:
        return kind == "V"
    out = subprocess.run([FFPROBE, "-v", "error", "-select_streams", kind, "-show_entries",
                          "stream=index", "-of", "csv=p=0", str(src)], capture_output=True, text=True)
    return bool(out.stdout.strip())


def has_audio_stream(src):
    return has_stream(src, "a")


def prepare_image(src, workdir):
    if src.suffix.lower() in (".heic", ".heif"):
        dest = workdir / "image.jpg"
        result = subprocess.run(["sips", "-s", "format", "jpeg", str(src), "--out", str(dest)],
                                capture_output=True, text=True)
        if result.returncode != 0:
            raise MediaError("Couldn't convert that HEIC photo. Try a JPEG or PNG.")
        return str(dest)
    return str(src)


def prepare_video(src, workdir):
    dest = workdir / "video.mp4"
    side = VIDEO_MAX_SIDE
    scale = f"scale='if(gt(iw,ih),min({side},iw),-2)':'if(gt(iw,ih),-2,min({side},ih))'"
    ffmpeg("-i", str(src), "-t", str(VIDEO_MAX_SECONDS), "-vf", scale, "-an",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p", str(dest))
    return str(dest)


# ---- attachments ----------------------------------------------------------
# Every attachment is converted once, when it's added, and kept for the whole chat:
# the full conversation (with its media) goes to the model on every turn.
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
AUDIO_EXT = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac", ".aiff", ".aif", ".caf", ".weba"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".mpg", ".mpeg", ".3gp"}
media_index = {}


def media_kind(filename, content_type):
    ext, kind = Path(filename or "").suffix.lower(), (content_type or "").lower()
    if kind.startswith("image/") or ext in IMAGE_EXT:
        return "image"
    if kind.startswith("audio/") or ext in AUDIO_EXT:
        return "audio"
    if kind.startswith("video/") or ext in VIDEO_EXT:
        return "video"
    return None


def wav_seconds(path):
    return round(max(0, Path(path).stat().st_size - 44) / 32000, 1)  # 16 kHz, mono, 16-bit


def prepare_media(src, kind, folder):
    if kind == "video" and not has_stream(src, "V") and has_audio_stream(src):
        kind = "audio"  # .webm, .mp4 and friends also hold sound-only recordings
    if kind == "image":
        path = prepare_image(src, folder)
        try:
            from PIL import Image
            with Image.open(path) as img:
                img.verify()
        except Exception:
            raise MediaError("Couldn't read that image. Try a JPEG or PNG.")
        return {"image": path}, {"kind": "image"}
    if kind == "audio":
        wav = to_wav(src, folder / "audio.wav")
        return {"audio": wav}, {"kind": "audio", "seconds": wav_seconds(wav)}
    video = prepare_video(src, folder)
    paths, info = {"video": video}, {"kind": "video", "soundtrack": False}
    if has_audio_stream(src):
        paths["soundtrack"] = to_wav(src, folder / "soundtrack.wav", VIDEO_MAX_SECONDS)
        info["soundtrack"] = True
    return paths, info


# ---- conversation ----------------------------------------------------------
DEFAULT_SYSTEM = (
    "You are LFM3-Omni, a helpful assistant running entirely on the user's Mac. "
    "You can see images and video, hear audio and speak. Keep answers concise."
)
MAX_TOOL_ROUNDS = 6
# Read out loud, a page of search results became a minute of speech, and long speech is where
# the model loses its place. Next to the results, this halved spoken answers (44-57 words in
# 12 of 12 tries); in the system prompt it worked half the time.
SPOKEN_NOTE = ("This answer will be spoken aloud. Sum up what you found in two or three short sentences "
               "and name at most three results. Don't read out titles, lists or links.")
MAX_MEDIA_IN_CONTEXT = 8
OLD_TOOL_RESULT_CHARS = 1200
active = {}        # conversation request id -> worker job id currently running
cancelled = set()  # conversation request ids the user stopped


def build_messages(history):
    """Turn the page's history into chat-template messages plus ordered media lists.

    The processor matches <|image|>, <|audio|> and <|video|> placeholders to files by
    order of appearance, so the lists are filled in the same order as the messages.

    Earlier turns are slimmed down: a voice message that has been transcribed goes in as
    its transcript, and tool results (whole web pages, say) are cut short. The current
    turn always goes in whole.
    """
    last_user = max((i for i, m in enumerate(history) if m.get("role") == "user"), default=-1)

    def as_text(i, mid):
        item = media_index.get(mid) or {}
        return i < last_user and item.get("voice") and item.get("transcript")

    order = [mid for i, m in enumerate(history) if m.get("role") == "user"
             for mid in (m.get("media") or []) if not as_text(i, mid)]
    keep = set(order[-MAX_MEDIA_IN_CONTEXT:])
    media = {"image": [], "audio": [], "video": []}
    messages = []
    for i, m in enumerate(history):
        role = m.get("role")
        if role == "user":
            content = []
            for mid in m.get("media") or []:
                item = media_index.get(mid)
                if item is None:
                    raise ValueError("An attachment from earlier in this chat is no longer available "
                                     "(the app was restarted). Start a new chat.")
                if as_text(i, mid):
                    content.append({"type": "text", "text": item["transcript"]})
                    continue
                if mid not in keep:
                    content.append({"type": "text", "text": f"[An earlier {item['kind']} is no longer shown.]"})
                    continue
                kind, paths = item["kind"], item["paths"]
                if kind == "video":
                    content.append({"type": "video"})
                    media["video"].append(paths["video"])
                    if "soundtrack" in paths:  # the video's sound, right after its frames
                        content.append({"type": "audio"})
                        media["audio"].append(paths["soundtrack"])
                        # Without this note the model ignores the sound whenever tools are offered.
                        content.append({"type": "text", "text": "[The audio above is this video's soundtrack.]"})
                else:
                    if kind == "audio" and not item.get("voice"):
                        # Frames an attached recording as material, not as the user talking. Without
                        # it, "Transcribe this" on a recording that says "remind me..." saves a reminder.
                        content.append({"type": "text", "text": "Here is a recording:"})
                    content.append({"type": kind})
                    media[kind].append(paths[kind])
            text = str(m.get("text") or "").strip()
            if text:
                content.append({"type": "text", "text": text})
            if content:
                messages.append({"role": "user", "content": content})
        elif role == "assistant" and m.get("tool_calls"):
            call = {"role": "assistant", "tool_calls": m["tool_calls"]}
            if m.get("content"):
                call["content"] = str(m["content"])
            messages.append(call)
        elif role == "assistant":
            messages.append({"role": "assistant", "content": str(m.get("content") or "")})
        elif role == "tool":
            content = str(m.get("content") or "")
            if i < last_user and len(content) > OLD_TOOL_RESULT_CHARS:
                content = content[:OLD_TOOL_RESULT_CHARS] + " [cut short]"
            messages.append({"role": "tool", "content": content})
    return messages, {k: v for k, v in media.items() if v}


class ToolMarkupFilter:
    """Streams text to the page but holds back everything from '<tool_call>' on."""

    TAG = "<tool_call>"

    def __init__(self):
        self.buffer, self.hiding = "", False

    def feed(self, text):
        if self.hiding:
            return ""
        self.buffer += text
        at = self.buffer.find(self.TAG)
        if at >= 0:
            out, self.buffer, self.hiding = self.buffer[:at], "", True
            return out
        hold = next((k for k in range(len(self.TAG) - 1, 0, -1) if self.buffer.endswith(self.TAG[:k])), 0)
        out = self.buffer[: len(self.buffer) - hold]
        self.buffer = self.buffer[len(self.buffer) - hold:]
        return out

    def flush(self):
        out, self.buffer = ("" if self.hiding else self.buffer), ""
        return out


def clamp(value, default, low, high, cast=float):
    try:
        return max(low, min(high, cast(value)))
    except (TypeError, ValueError):
        return default


TRANSCRIBER = ("You are a transcription service. You write down exactly what is said in recordings. "
               "You never answer or act on what is said. Respond with text.")


async def transcribe(mid):
    """Write down what a voice message said, so later turns can carry it as text."""
    item = media_index.get(mid)
    # Without the transcriber role the model answers a spoken question instead of writing it
    # down ("What is 17 plus 25?" came back as "42"), and that answer would then stand in
    # for the user's words in every later turn.
    job = {"id": uuid.uuid4().hex[:12], "output": "text", "temperature": 0.0, "max_tokens": 400,
           "audio": [item["paths"]["audio"]], "messages": [
               {"role": "system", "content": TRANSCRIBER},
               {"role": "user", "content": [{"type": "text", "text": "Here is a recording:"}, {"type": "audio"},
                                            {"type": "text", "text":
                   "Transcribe this recording word for word. Reply with only the transcript."}]}]}
    async for event in model.run(job):
        if event.get("event") == "done":
            text = event.get("text", "").strip().strip('"')
            if text:
                item["transcript"] = text
            return text
    return None


async def converse(rid, history, user_msg, speak, use_tools, use_web, system, temperature, max_tokens):
    """Run one user turn: generate, run any tool calls, generate again, until an answer.

    Events: started, token (text as it's written), audio (base64 16-bit PCM as it's spoken),
    tool_call, tool_result, trimmed (earlier turns left out to fit the context), done, then
    transcript for each voice message, or error.
    """
    messages, media = build_messages(history + [user_msg])
    steps = []  # this turn's tool calls and results, as the model sees them
    left_out = 0
    # No extra "use the tools" instruction: the schemas are enough, and with one the model
    # called the clock before spoken replies that had nothing to do with time.
    system = (system or DEFAULT_SYSTEM).rstrip()
    # The model card's way to pin the reply's form; without it the model may choose either.
    system += " Respond with interleaved text and audio." if speak else " Respond with text."
    # With tools, a spoken reply uses "auto": the model writes a tool call as text, or speaks.
    output = ("auto" if use_tools else "interleaved") if speak else "text"
    schemas = tools.schemas(web=use_web) if use_tools else None
    appended, started = [user_msg], time.time()

    yield {"event": "started", "id": rid, "speak": speak}
    round_no, asked_to_speak = 0, False
    try:
        while True:
            job = {"id": uuid.uuid4().hex[:12], "messages": [{"role": "system", "content": system}] + messages + steps,
                   "tools": schemas, "output": output, "stream": speak,
                   "temperature": temperature, "max_tokens": max_tokens, **media,
                   # With earlier turns to leave out, make room for a whole reply, not a scrap of one.
                   "min_reply_tokens": max_tokens if history else 0}
            active[rid] = job["id"]
            hide = ToolMarkupFilter()
            # In "auto" the model may write an answer instead of speaking it, and then it's asked
            # again; its words wait here until speech starts, so the page never shows a draft.
            held, speaking = [], output != "auto"
            text, audio, stats, failure, too_long = "", None, None, None, None
            async for event in model.run(job):
                kind = event.get("event")
                if kind == "token":
                    visible = hide.feed(event["text"])
                    if visible and speaking:
                        yield {"event": "token", "text": visible}
                    elif visible:
                        held.append(visible)
                elif kind == "audio_chunk":
                    if not speaking:
                        speaking = True
                        if held:
                            yield {"event": "token", "text": "".join(held)}
                            held = []
                    yield {"event": "audio", "pcm": event["pcm"], "rate": event["rate"]}
                elif kind == "done":
                    text, audio, stats = event.get("text", ""), event.get("audio"), event.get("stats")
                elif kind == "error":
                    failure = event.get("message")
                    too_long = event.get("code") == "context" and event.get("prompt_tokens")
            if failure and too_long and history and rid not in cancelled:
                # Leave out the earliest turns (each a user message and the replies after it) and
                # retry. Leave out enough at once, judged by their share of the text, since every
                # try prepares the whole prompt again, images and video included.
                over = (too_long + max_tokens - CONTEXT_TOKENS) / too_long
                sizes = [len(json.dumps(m, ensure_ascii=False)) + 6000 * len(m.get("media") or []) for m in history]
                cut, dropped = 0, 0
                while cut < len(history) and (cut == 0 or dropped < over * 1.15 * sum(sizes)):
                    cut = next((i for i in range(cut + 1, len(history)) if history[i].get("role") == "user"), len(history))
                    dropped = sum(sizes[:cut])
                history, left_out = history[cut:], left_out + cut
                messages, media = build_messages(history + [user_msg])
                model.log(f"The prompt was {too_long:,} tokens, more than the {event.get('context_tokens', 32768) // 1024}K context; "
                          f"left out the {left_out} earliest messages and tried again.")
                yield {"event": "trimmed", "left_out": left_out, "prompt_tokens": too_long}
                continue
            if failure and too_long:
                yield {"event": "error", "message": f"This message is too long for the model: {too_long:,} tokens, and it "
                       "holds 32K for the prompt and the reply together. Use a shorter video or fewer images."}
                return
            if failure:
                yield {"event": "error", "message": failure}
                return
            rest = "".join(held) + hide.flush()

            calls = tools.parse_tool_calls(text) if use_tools and rid not in cancelled else []
            if speak and output == "auto" and not calls and not audio and not asked_to_speak and rid not in cancelled:
                # Free to choose, it answered in writing; ask once more for the spoken version.
                output, asked_to_speak = "interleaved", True
                continue
            if rest:
                yield {"event": "token", "text": rest}
            if not calls or round_no == MAX_TOOL_ROUNDS:
                answer = tools.strip_tool_markup(text)
                if calls and not answer:
                    answer = "I stopped after several tool calls without reaching an answer."
                appended.append({"role": "assistant", "content": answer})
                stats = {**(stats or {}), "generate_seconds": (stats or {}).get("seconds"),
                         "seconds": round(time.time() - started, 2), "tool_rounds": round_no}
                if rid in cancelled:
                    stats["finish"] = "cancelled"
                yield {"event": "done", "text": answer, "audio": audio, "stats": stats, "append": appended}
                break

            preface = tools.strip_tool_markup(text)
            results = []
            for call in calls:
                fn = call["function"]
                yield {"event": "tool_call", "name": fn["name"], "arguments": fn["arguments"]}
                result = await tools.run_tool(call, web=use_web)
                results.append(tools.for_model(result))
                shot = result.get("_screenshot") if isinstance(result, dict) else None
                yield {"event": "tool_result", "name": fn["name"], "result": tools.for_model(result),
                       **({"screenshot": f"/outputs/{shot}"} if shot else {})}
            call_msg = {"role": "assistant", "tool_calls": calls, **({"content": preface} if preface else {})}
            tool_msg = {"role": "tool", "content": json.dumps(results, ensure_ascii=False)}
            seen = tool_msg
            if speak and any(c["function"]["name"] in tools.WEB_TOOLS for c in calls):
                seen = {"role": "tool", "content": json.dumps({"results": results, "note": SPOKEN_NOTE}, ensure_ascii=False)}
            steps += [call_msg, seen]
            appended += [call_msg, tool_msg]
            if speak:
                # A spoken reply answers right after one tool step: left to choose, the model
                # tends to write, and after a search it would go on to open pages, which keeps
                # someone waiting in silence for ten seconds or more. Written replies can chain.
                output = "interleaved"
            round_no += 1
    finally:
        active.pop(rid, None)
        cancelled.discard(rid)

    # The model is idle while the page plays the reply: write down what each voice message said.
    for mid in user_msg["media"]:
        item = media_index.get(mid) or {}
        if item.get("voice") and not item.get("transcript"):
            text = await transcribe(mid)
            if text:
                yield {"event": "transcript", "media": mid, "text": text}


# ---- read aloud --------------------------------------------------------------
SCRIPTS = [  # (language, regex for its letters); Latin-script text falls back to the chosen language
    ("Arabic", r"[؀-ۿݐ-ݿ]"), ("Hebrew", r"[֐-׿]"), ("Greek", r"[Ͱ-Ͽ]"),
    ("Korean", r"[가-힯ᄀ-ᇿ]"), ("Japanese", r"[぀-ヿ]"), ("Chinese", r"[一-鿿]"),
]


def guess_language(text, fallback):
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return fallback
    for language, pattern in SCRIPTS:
        if sum(1 for c in letters if re.match(pattern, c)) / len(letters) > 0.4:
            return language
    return fallback


def plain_text(markdown):
    text = re.sub(r"```.*?```", " ", markdown, flags=re.DOTALL)
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    text = re.sub(r"^\s*(#{1,6}|[-*•])\s+", "", text, flags=re.MULTILINE)
    return re.sub(r"[*_`#>]", "", text).strip()


# ---- web app -------------------------------------------------------------
model = ModelProcess()


def clean_data_folders():
    shutil.rmtree(MEDIA, ignore_errors=True)
    MEDIA.mkdir(parents=True, exist_ok=True)
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - 24 * 3600
    for old in [*OUTPUTS.glob("*.wav"), *OUTPUTS.glob("page-*.jpg")]:  # speech and page screenshots
        if old.stat().st_mtime < cutoff:
            old.unlink(missing_ok=True)


@asynccontextmanager
async def lifespan(_app):
    clean_data_folders()
    if not FFMPEG:
        model.log("ffmpeg wasn't found: audio, recording and video won't work. Install it with: brew install ffmpeg")
    if os.environ.get("LFM3_AUTOSTART") == "1":
        await model.start()
    yield
    await model.stop()
    await browser.browser.close()


app = FastAPI(title="LFM3-Omni Lab", lifespan=lifespan)
not_running = {"error": "The model isn't running. Start it, then try again."}


@app.get("/api/model")
async def get_model():
    return model.status()


@app.post("/api/model/{action}")
async def control_model(action: str):
    if action == "start":
        await model.start()
    elif action == "stop":
        await model.stop()
    elif action == "restart":
        await model.restart()
    else:
        return JSONResponse({"error": f"Unknown action: {action}"}, status_code=404)
    return model.status()


@app.get("/api/logs")
async def get_logs(since: int = 0):
    return {"lines": [entry for entry in model.logs if entry["n"] > since], "last": model.log_seq}


@app.get("/api/options")
async def get_options():
    return {"tts_languages": TTS_LANGUAGES, "voices": VOICES, "tools": tools.DESCRIPTIONS,
            "default_system": DEFAULT_SYSTEM, "video_max_seconds": VIDEO_MAX_SECONDS,
            "video_max_side": VIDEO_MAX_SIDE, "max_media_in_context": MAX_MEDIA_IN_CONTEXT}


@app.post("/api/media")
async def add_media(request: Request):
    form = await request.form()
    upload = form.get("file")
    if not isinstance(upload, UploadFile) or not upload.filename:
        return JSONResponse({"error": "No file was sent."}, status_code=400)
    kind = media_kind(upload.filename, upload.content_type)
    if kind is None:
        return JSONResponse({"error": "Attach an image, an audio recording or a video."}, status_code=400)
    mid = uuid.uuid4().hex[:12]
    folder = MEDIA / mid
    folder.mkdir(parents=True)
    src = folder / f"original{Path(upload.filename).suffix.lower()[:10]}"
    with src.open("wb") as handle:
        shutil.copyfileobj(upload.file, handle)
    try:
        paths, info = await asyncio.to_thread(prepare_media, src, kind, folder)
    except MediaError as exc:
        shutil.rmtree(folder, ignore_errors=True)
        return JSONResponse({"error": str(exc)}, status_code=400)
    item = {"id": mid, "name": upload.filename, "voice": form.get("voice") == "1", **info}
    media_index[mid] = {**item, "paths": paths}
    return item


@app.post("/api/chat")
async def chat(request: Request):
    if model.state != "ready":
        return JSONResponse(not_running, status_code=409)
    body = await request.json()
    message = body.get("message") or {}
    user_msg = {"role": "user", "text": str(message.get("text") or "").strip(),
                "media": [str(m) for m in message.get("media") or []]}
    if not user_msg["text"] and not user_msg["media"]:
        return JSONResponse({"error": "Type a message or attach something first."}, status_code=400)
    reply = body.get("reply", "auto")
    spoke = any(media_index.get(m, {}).get("voice") for m in user_msg["media"])
    speak = reply == "voice" or (reply == "auto" and spoke)
    history = body.get("history") or []
    try:
        build_messages(history + [user_msg])  # fail fast on missing attachments
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    rid = uuid.uuid4().hex[:12]
    use_tools = bool(body.get("tools", True))
    turn = converse(rid, history, user_msg, speak, use_tools, use_tools and bool(body.get("web", False)),
                    str(body.get("system") or "").strip(),
                    clamp(body.get("temperature"), 0.0, 0.0, 1.5),
                    clamp(body.get("max_tokens"), 1024, 16, 8192, int))

    async def stream():
        async for event in turn:
            yield json.dumps(event, ensure_ascii=False) + "\n"

    return StreamingResponse(stream(), media_type="application/x-ndjson")


@app.post("/api/cancel/{rid}")
async def cancel(rid: str):
    if rid in active:
        cancelled.add(rid)
        model.cancel(active[rid])
    return {"ok": True}


@app.post("/api/speak")
async def speak(request: Request):
    if model.state != "ready":
        return JSONResponse(not_running, status_code=409)
    body = await request.json()
    text = plain_text(str(body.get("text") or ""))[:2000]
    if not text:
        return JSONResponse({"error": "There's no text to read aloud."}, status_code=400)
    voice = body.get("voice", "female")
    language = body.get("language", "auto")
    language = guess_language(text, "American English") if language == "auto" else language
    if language not in TTS_LANGUAGES or voice not in VOICES:
        return JSONResponse({"error": "Pick a language and voice from the list."}, status_code=400)
    job = {"id": uuid.uuid4().hex[:12], "output": "audio", "max_tokens": 4096, "messages": [
        {"role": "system", "content": f"Perform TTS in {language} using a {voice} voice."},
        {"role": "user", "content": text}]}
    async for event in model.run(job):
        if event.get("event") == "done":
            return {"audio": event.get("audio"), "language": language, "stats": event.get("stats")}
        if event.get("event") == "error":
            return JSONResponse({"error": event.get("message")}, status_code=500)
    return JSONResponse({"error": "The model stopped before finishing."}, status_code=500)


@app.get("/api/reminders")
async def get_reminders():
    return tools.list_reminders()


@app.delete("/api/reminders")
async def delete_reminders():
    tools.clear_reminders()
    return {"ok": True}


OUTPUTS.mkdir(parents=True, exist_ok=True)
app.mount("/outputs", StaticFiles(directory=OUTPUTS), name="outputs")
app.mount("/", StaticFiles(directory=ROOT / "static", html=True), name="static")

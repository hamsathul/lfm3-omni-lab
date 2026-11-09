"""Model worker for llama.cpp: runs LFM3-Omni's GGUF in llama-server and answers the lab's jobs.

It speaks the same protocol as app/worker.py (see there), so the server treats both alike. It's
the backend on Windows and Linux, and on a Mac with LFM3_BACKEND=llamacpp.

llama-server writes text only. A spoken reply is therefore written first, and each sentence is
spoken with llama-server's /tts as soon as it's complete, while the rest is still being
written: speech starts about a sentence late. Read aloud goes straight to /tts.

llama-server runs as this worker's child with two slots of 32K context each. The conversation
always uses the first, and speech, which runs while the reply is still being written, takes
the other one. Its full log goes to data/llama-server.log; warnings, errors and the device it
runs on also go to the model log.
"""

import argparse
import base64
import http.client
import io
import json
import os
import queue
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run as a script: make `app` importable
from app.guards import CONTEXT_TOKENS, PromptTooLong, SpeechWatch, reply_budget, speakable_units  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GGUF_REPO = "LiquidAI/LFM3-Omni-2B-GGUF"
DEFAULT_QUANT = "Q8_0"
CHAT_SLOT, SLOTS = 0, 2
RATE = 24000  # /tts speaks at 24 kHz
FRAME_SECONDS = 0.08  # one speech frame
NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW

# The lab's language names (server.TTS_LANGUAGES) as /tts language codes.
LANG_CODES = {
    "American English": "en", "British English": "en-gb", "English": "en", "Arabic": "ar", "Chinese": "zh",
    "Danish": "da", "Dutch": "nl", "Finnish": "fi", "French": "fr", "German": "de", "Greek": "el",
    "Hebrew": "he", "Italian": "it", "Japanese": "ja", "Korean": "ko", "Polish": "pl", "Portuguese": "pt",
    "Spanish": "es", "Swedish": "sv", "Turkish": "tr",
}

# Keep the real stdout for the protocol; send every stray print to stderr.
_proto = os.fdopen(os.dup(1), "w", buffering=1, encoding="utf-8")
os.dup2(2, 1)
sys.stdout = sys.stderr
_send_lock = threading.Lock()


def send(**message):
    with _send_lock:
        _proto.write(json.dumps(message) + "\n")
        _proto.flush()


def log(text):
    print(f"[worker] {text}", file=sys.stderr, flush=True)


# ---- finding the program and the model ------------------------------------------------
def find_llama_server():
    exe = "llama-server.exe" if os.name == "nt" else "llama-server"
    chosen = os.environ.get("LFM3_LLAMA_SERVER")
    candidates = [Path(chosen)] if chosen else []
    candidates += [ROOT / "llama.cpp" / exe, ROOT / "llama.cpp" / "bin" / exe]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("llama-server")


def find_ffmpeg_dir():
    found = shutil.which("ffmpeg")
    if not found and os.name == "nt":
        links = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Links" / "ffmpeg.exe"
        found = str(links) if links.is_file() else None
    return str(Path(found).parent) if found else None


def _quant_of(name):
    m = re.search(r"-((?:Q|IQ)\d[\w]*|BF16|F16|F32)\.gguf$", name, re.IGNORECASE)
    return m.group(1).upper() if m else ""


def model_files(model):
    """(model.gguf, mmproj.gguf) from a .gguf file, a folder holding both, or a Hugging Face
    repo id with an optional quantization (LiquidAI/LFM3-Omni-2B-GGUF:Q4_K_M)."""
    path = Path(model)
    if path.is_file() or path.is_dir():
        folder = path.parent if path.is_file() else path
        ggufs = sorted(folder.glob("*.gguf"))
        projectors = [p for p in ggufs if p.name.lower().startswith("mmproj")]
        weights = [p for p in ggufs if p not in projectors]
        if path.is_file():
            weights = [path]
        if not weights or not projectors:
            raise FileNotFoundError(f"{folder} needs the model's .gguf and its mmproj-*.gguf.")
        main = next((w for w in weights if _quant_of(w.name) == DEFAULT_QUANT), weights[0])
        quant = _quant_of(main.name)
        projector = next((p for p in projectors if _quant_of(p.name) == quant), projectors[0])
        return str(main), str(projector)
    repo, _, quant = model.partition(":")
    quant = (quant or DEFAULT_QUANT).upper()
    name = repo.split("/")[-1].removesuffix("-GGUF")
    from huggingface_hub import hf_hub_download
    files = []
    for filename in (f"{name}-{quant}.gguf", f"mmproj-{name}-{quant}.gguf"):
        try:
            files.append(hf_hub_download(repo, filename, local_files_only=True))
        except Exception:
            log(f"Downloading {filename} from Hugging Face...")
            files.append(hf_hub_download(repo, filename))
    return files[0], files[1]


# ---- memory --------------------------------------------------------------------------
def gpu_memory_mb():
    """Memory in use on the first NVIDIA GPU, or None without one."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run([smi, "--query-gpu=memory.used", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW).stdout
        return float(out.split()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def process_memory_gb(pid):
    try:
        if os.name == "nt":
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], capture_output=True,
                                 text=True, timeout=5, creationflags=NO_WINDOW).stdout
            kb = re.sub(r"[^\d]", "", out.strip().split('","')[-1]) if out.strip().startswith('"') else ""
            return round(int(kb) * 1024 / 1e9, 2) if kb else None
        out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=5).stdout
        return round(int(out.strip()) * 1024 / 1e9, 2)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def _die_with_parent():
    """Linux: llama-server gets SIGTERM when this worker dies, however it dies."""
    import ctypes
    import signal
    ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG


def _kill_with_worker(proc):
    """Windows: put llama-server in a job that ends it when this worker ends, even if the
    worker is killed outright (its children would otherwise live on, holding the GPU).
    The returned handle has to stay open for the worker's life."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    job = kernel32.CreateJobObjectW(None, None)
    info = Extended()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info)) and \
        kernel32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(int(proc._handle)))
    if not ok:
        log(f"Couldn't tie llama-server to this worker (error {ctypes.get_last_error()}); "
            "if the worker is killed, end llama-server.exe by hand.")
    return job


# ---- talking to llama-server ----------------------------------------------------------
class ServerError(Exception):
    pass


class Stream:
    """One streamed request to llama-server, read on its own thread so a cancel can cut it
    off at any moment, even while the prompt is still being read. Closing the connection
    makes llama-server stop generating."""

    def __init__(self, port, path, body):
        self.items = queue.Queue()
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=900)
        self.closed = False
        threading.Thread(target=self._read, args=(path, body), daemon=True).start()

    def _read(self, path, body):
        try:
            self.conn.request("POST", path, json.dumps(body).encode(), {"Content-Type": "application/json"})
            response = self.conn.getresponse()
            if response.status != 200:
                self.items.put(("status", response.status, response.read()))
                return
            self.items.put(("open", response.getheader("Content-Type", ""), None))
            if path == "/tts":
                while chunk := response.read1(65536):
                    self.items.put(("bytes", chunk, None))
            else:
                for raw in response:
                    line = raw.decode("utf-8", "replace").strip()
                    if line.startswith("data: ") and line != "data: [DONE]":
                        self.items.put(("event", json.loads(line[6:]), None))
            self.items.put(("end", None, None))
        except Exception as exc:  # noqa: BLE001 - reported to the reader
            self.items.put(("end", None, None) if self.closed else ("failed", exc, None))

    def get(self, cancelled):
        """The next item, or None once `cancelled()` says to stop."""
        while True:
            if cancelled():
                self.close()
                return None
            try:
                return self.items.get(timeout=0.1)
            except queue.Empty:
                continue

    def close(self):
        self.closed = True
        try:
            if self.conn.sock is not None:
                self.conn.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.conn.close()


def error_of(status, raw):
    """A useful exception from llama-server's error body."""
    try:
        err = json.loads(raw).get("error") or {}
    except (ValueError, AttributeError):
        err = {}
    if err.get("type") == "exceed_context_size_error":
        return PromptTooLong(int(err.get("n_prompt_tokens") or CONTEXT_TOKENS + 1))
    return ServerError(err.get("message") or f"llama-server answered {status}: {raw[:200]!r}")


# ---- turning the lab's messages into llama-server's ----------------------------------
IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".bmp": "image/bmp", ".gif": "image/gif"}


class Media:
    """Files as base64 data, kept for the few files a conversation sends again every turn."""

    def __init__(self):
        self.cache = {}

    def data(self, path):
        path = Path(path)
        key = (str(path), path.stat().st_mtime_ns)
        if key not in self.cache:
            if len(self.cache) > 24:
                self.cache.pop(next(iter(self.cache)))
            raw = path.read_bytes()
            mime = IMAGE_TYPES.get(path.suffix.lower())
            if path.suffix.lower() in (".webp", ".tif", ".tiff", ".heic", ".heif"):
                # Formats llama.cpp's image reader doesn't take go as PNG.
                from PIL import Image
                with Image.open(path) as img:
                    buf = io.BytesIO()
                    img.convert("RGB").save(buf, "PNG")
                raw, mime = buf.getvalue(), "image/png"
            self.cache[key] = (base64.b64encode(raw).decode(), mime)
        return self.cache[key]

    def part(self, kind, path):
        data, mime = self.data(path)
        if kind == "image":
            return {"type": "image_url", "image_url": {"url": f"data:{mime or 'image/png'};base64,{data}"}}
        if kind == "audio":
            return {"type": "input_audio", "input_audio": {"data": data, "format": "wav"}}
        return {"type": "input_video", "input_video": {"data": data}}


def convert(messages, job, media):
    """The lab's chat-template messages and ordered media lists as OpenAI-style messages."""
    pending = {kind: list(job.get(kind) or []) for kind in ("image", "audio", "video")}
    out, last_call = [], None
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if role == "user" and isinstance(content, list):
            parts = []
            for part in content:
                kind = part.get("type")
                if kind == "text":
                    parts.append({"type": "text", "text": part.get("text", "")})
                elif kind in pending and pending[kind]:
                    parts.append(media.part(kind, pending[kind].pop(0)))
            out.append({"role": "user", "content": parts})
        elif role == "assistant" and message.get("tool_calls"):
            calls = []
            for n, call in enumerate(message["tool_calls"]):
                fn = call.get("function") or {}
                arguments = fn.get("arguments") or {}
                last_call = call.get("id") or f"call_{len(out)}_{n}"
                calls.append({"id": last_call, "type": "function", "function": {
                    "name": fn.get("name", ""),
                    "arguments": arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)}})
            out.append({"role": "assistant", "content": str(content or ""), "tool_calls": calls})
        elif role == "tool":
            out.append({"role": "tool", "tool_call_id": last_call or "call_0", "content": str(content or "")})
        else:
            out.append({"role": role, "content": content if isinstance(content, str) else str(content or "")})
    return out


def tool_markup(calls):
    """Tool calls as the text the MLX model writes, which the server parses. Values are JSON,
    so a string stays a string and a number a number."""
    text = ""
    for call in calls:
        try:
            arguments = json.loads(call["arguments"] or "{}")
        except json.JSONDecodeError:
            log(f"Ignored a tool call with broken arguments: {call['name']} {call['arguments'][:120]}")
            continue
        params = "".join(f"<parameter={k}>\n{json.dumps(v, ensure_ascii=False)}\n</parameter>\n"
                         for k, v in (arguments or {}).items())
        text += f"<tool_call>\n<function={call['name']}>\n{params}</function>\n</tool_call>"
    return text


# ---- speech ---------------------------------------------------------------------------
class Sentences:
    """Cuts streamed text into pieces to speak as soon as each sentence is complete. The first
    goes as soon as it can; later ones gather to about a sentence and a half, which sounds
    more natural and asks /tts less often."""

    END = re.compile(r"[.!?…]+[\"'”’)\]]*\s+|[。！？]+|\n+")
    ABBREVIATIONS = {"mr", "mrs", "ms", "dr", "st", "vs", "etc", "e.g", "i.e", "no", "jr", "sr", "prof", "approx", "fig", "inc", "ltd"}

    def __init__(self):
        self.buffer, self.spoken = "", 0

    def feed(self, text):
        self.buffer += text
        out, start = [], 0
        while m := self.END.search(self.buffer, start):
            piece = self.buffer[:m.end()]
            word = re.search(r"([\w.]+)[.!?…]+[\"'”’)\]]*\s*$", piece)
            abbreviation = word and (word.group(1).lower() in self.ABBREVIATIONS or len(word.group(1)) == 1)
            least = 12 if self.spoken == 0 else 60
            if not abbreviation and len(piece.strip()) >= least or "\n" in m.group(0) and piece.strip():
                out.append(piece.strip())
                self.buffer, start = self.buffer[m.end():], 0
                self.spoken += 1
            else:
                start = m.end()
        if len(self.buffer) > 280:  # a long run without a full stop: break at a comma
            cut = max(self.buffer.rfind(", ", 120), self.buffer.rfind("; ", 120), self.buffer.rfind(" ", 200))
            if cut > 0:
                out.append(self.buffer[:cut + 1].strip())
                self.buffer = self.buffer[cut + 1:]
                self.spoken += 1
        return out

    def flush(self):
        rest, self.buffer = self.buffer.strip(), ""
        return [rest] if rest else []


def speakable(text):
    """Text as it should be said: no Markdown, links, code or emoji."""
    text = re.sub(r"```.*?(```|$)", " ", text, flags=re.DOTALL)
    text = re.sub(r"<tool_call>.*?(</tool_call>|$)", " ", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"^\s*(#{1,6}|[-*•+]|\d+[.)])\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"[*_#>|~]", "", text)
    text = re.sub("[\U0001F000-\U0001FAFF☀-➿️‍]", "", text)
    return " ".join(text.split())


SCRIPT_LANGS = [("ar", r"[؀-ۿݐ-ݿ]"), ("he", r"[֐-׿]"), ("el", r"[Ͱ-Ͽ]"),
                ("ko", r"[가-힯ᄀ-ᇿ]"), ("ja", r"[぀-ヿ]"), ("zh", r"[一-鿿]")]
COMMON_WORDS = {
    "en": "the and is are of to in it you that for with on this was what",
    "es": "el la los las de que y es en un una por para con no se del está son",
    "fr": "le la les de des et est un une que pour dans pas ce il elle je vous du au",
    "de": "der die das und ist nicht ein eine ich sie mit zu den von es auf für wir",
    "it": "il lo la gli le di che è e un una per non sono con del della",
    "pt": "o a os as de que e é um uma para não com do da em está são",
    "nl": "de het een en is van niet dat ik je met op zijn voor er",
    "sv": "och är att det en ett som på för med inte jag har av till",
    "da": "og er at det en et som på for med ikke jeg har af til",
    "fi": "ja on ei se että olen hän mutta kun tämä oli ovat",
    "pl": "i w nie na się to jest że z do jak co ale",
    "tr": "ve bir bu da de ne için ile çok ben sen değil var",
}
COMMON_WORDS = {lang: set(words.split()) for lang, words in COMMON_WORDS.items()}


def guess_lang(text):
    """The /tts language code for a reply: by its script, else by its commonest words."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "en"
    for lang, pattern in SCRIPT_LANGS:
        if sum(1 for c in letters if re.match(pattern, c)) / len(letters) > 0.3:
            return "ja" if lang == "zh" and re.search(r"[぀-ヿ]", text) else lang
    words = re.findall(r"\w+", text.lower())
    scores = {lang: sum(w in common for w in words) for lang, common in COMMON_WORDS.items()}
    best = max(scores, key=scores.get)
    return best if best != "en" and scores[best] >= 2 and scores[best] > scores["en"] else "en"


def pcm16(floats):
    import numpy as np
    return (np.clip(floats, -1, 1) * 32767).astype("<i2")


def save_wav(path, samples):
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes(samples.tobytes())


# ---- the worker -------------------------------------------------------------------------
class Worker:
    def __init__(self, args):
        self.outputs = Path(args.outputs)
        self.outputs.mkdir(parents=True, exist_ok=True)
        self.model = args.model
        self.media = Media()
        self.cancelled = set()
        self.proc = None
        self.port = None
        self.gpu_baseline = None
        self.no_speech = []
        self.stopping = False

    # -- llama-server --
    def start(self):
        server = find_llama_server()
        if not server:
            raise FileNotFoundError("llama-server wasn't found. Run the start script again to download llama.cpp, "
                                    "or set LFM3_LLAMA_SERVER to its path.")
        weights, projector = model_files(self.model)
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        cmd = [server, "-m", weights, "--mmproj", projector, "--host", "127.0.0.1", "--port", str(self.port),
               "-c", str(CONTEXT_TOKENS * SLOTS), "-np", str(SLOTS), "--reasoning", "off", "--no-webui",
               "--log-colors", "off"]
        ffmpeg_dir = find_ffmpeg_dir()
        if ffmpeg_dir:
            cmd += ["--video-ffmpeg-dir", ffmpeg_dir]
        extra = os.environ.get("LFM3_LLAMA_ARGS", "").strip()
        if extra:
            cmd += shlex.split(extra, posix=os.name != "nt")
        self.gpu_baseline = gpu_memory_mb()
        log(f"Starting llama-server: {Path(weights).name} with {Path(projector).name}")
        self.log_file = open(self.outputs.parent / "llama-server.log", "w", encoding="utf-8", errors="replace")
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     creationflags=NO_WINDOW, preexec_fn=_die_with_parent if sys.platform.startswith("linux") else None)
        self._job = _kill_with_worker(self.proc)
        threading.Thread(target=self._follow_log, daemon=True).start()
        deadline = time.time() + 300
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError(f"llama-server stopped while loading (exit code {self.proc.returncode}). "
                                   "See data/llama-server.log.")
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
                conn.request("GET", "/health")
                if conn.getresponse().status == 200:
                    break
            except OSError:
                pass
            time.sleep(0.25)
        else:
            raise RuntimeError("llama-server didn't get ready within 5 minutes. See data/llama-server.log.")
        threading.Thread(target=self._watch, daemon=True).start()
        self.no_speech = self._speech_tokens()

    def _follow_log(self):
        # Lines start with a time and a level letter: "0.01.481.893 W ...".
        keep = re.compile(r"^\S+ [EW] |error|failed|using device|CUDA\d|Vulkan\d|Metal|offloaded \d+/\d+|model loaded",
                          re.IGNORECASE)
        # Repeated on every request, or harmless here (it listens on 127.0.0.1 only).
        quiet = re.compile(r"print_timing|slot |srv +log_server_r|update_slots|params_from_|get_availabl|launch_slot|release:"
                           r"|embeddings required|no API key|preserving reasoning|cancel task|experimental stage|may have reduced")
        for raw in self.proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip()
            self.log_file.write(line + "\n")
            self.log_file.flush()
            if keep.search(line) and not quiet.search(line):
                print(f"[llama-server] {line[:300]}", file=sys.stderr, flush=True)

    def _watch(self):
        code = self.proc.wait()
        if not self.stopping:
            send(event="fatal", message=f"llama-server stopped unexpectedly (exit code {code}). See data/llama-server.log.")
            log(f"llama-server exited with code {code}")
            os._exit(1)

    def stop(self):
        self.stopping = True
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def _speech_tokens(self):
        """Token ids that open speech: a written reply never gets them (the MLX worker does the same)."""
        ids = []
        for token in ("<|audio_start|>", "<|mixed_start|>"):
            try:
                conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
                conn.request("POST", "/tokenize", json.dumps({"content": token, "parse_special": True}),
                             {"Content-Type": "application/json"})
                found = json.loads(conn.getresponse().read()).get("tokens") or []
                if len(found) == 1:
                    ids.append(found[0])
            except (OSError, ValueError):
                pass
        return ids

    def memory_gb(self):
        used = gpu_memory_mb()
        if used is not None and self.gpu_baseline is not None:
            return round(max(0.0, used - self.gpu_baseline) / 1024, 2)
        return process_memory_gb(self.proc.pid) if self.proc else None

    # -- jobs --
    def is_cancelled(self, job_id):
        return lambda: job_id in self.cancelled

    def run(self, job):
        if job.get("output", "text") == "audio":
            return self.read_aloud(job)
        return self.chat(job)

    def chat(self, job):
        job_id = job["id"]
        output = job.get("output", "text")
        spoken = output in ("interleaved", "auto")
        messages = [dict(m) for m in job["messages"]]
        if spoken and messages and messages[0].get("role") == "system":
            # It can only write here; what it writes is then spoken. A hint to write "the way you'd
            # say it" cost tool calls (2 of 4 questions that needed one, against 4 of 4 without),
            # and answers were as short without it. Markdown is left out when speaking anyway.
            system = str(messages[0].get("content") or "")
            messages[0]["content"] = system.replace("Respond with interleaved text and audio.", "Respond with text.")
        body = {"messages": convert(messages, job, self.media), "stream": True, "return_progress": True,
                "temperature": float(job.get("temperature") or 0.0), "max_tokens": int(job.get("max_tokens") or 1024),
                "cache_prompt": True, "id_slot": CHAT_SLOT}
        if job.get("tools"):
            body["tools"] = job["tools"]
        if self.no_speech:
            body["logit_bias"] = [[token, False] for token in self.no_speech]
        least = int(job.get("min_reply_tokens") or 0)
        started = time.time()
        voice = Voice(self, job_id, stream=bool(job.get("stream")), started=started) if spoken else None
        sentences = Sentences() if spoken else None

        stream = Stream(self.port, "/v1/chat/completions", body)
        text, calls, prompt_tokens, timings, finish = "", {}, None, {}, None
        cancelled = self.is_cancelled(job_id)
        try:
            while (item := stream.get(cancelled)) is not None:
                kind, value, extra = item
                if kind == "status":
                    raise error_of(value, extra)
                if kind == "failed":
                    raise ServerError(f"Lost llama-server mid-answer: {value}")
                if kind == "end":
                    break
                if kind != "event":
                    continue
                if "prompt_progress" in value and prompt_tokens is None:
                    prompt_tokens = int(value["prompt_progress"].get("total") or 0)
                    reply_budget(prompt_tokens, body["max_tokens"], least)  # PromptTooLong when it can't fit
                if value.get("timings"):
                    timings = value["timings"]
                for choice in value.get("choices") or []:
                    delta = choice.get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        text += piece
                        send(event="token", id=job_id, text=piece)
                        if voice:
                            for sentence in sentences.feed(piece):
                                voice.say(sentence)
                    for call in delta.get("tool_calls") or []:
                        slot = calls.setdefault(call.get("index", 0), {"name": "", "arguments": ""})
                        fn = call.get("function") or {}
                        slot["name"] += fn.get("name") or ""
                        slot["arguments"] += fn.get("arguments") or ""
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
            else:
                finish = "cancelled"
        except BaseException:
            stream.close()
            if voice:
                voice.abandon()
            raise
        if voice:
            if finish != "cancelled":
                for sentence in sentences.flush():
                    voice.say(sentence)
            voice.finish(cancelled)
        markup = tool_markup([calls[i] for i in sorted(calls)])
        prompt_tokens = prompt_tokens or int(timings.get("prompt_n", 0)) + int(timings.get("cache_n", 0))
        generated = int(timings.get("predicted_n") or 0)
        elapsed = time.time() - started
        if finish == "tool_calls":
            finish = "stop"
        stats = {
            "seconds": round(elapsed, 2),
            "prompt_tokens": prompt_tokens,
            "tokens": generated,
            "tokens_per_sec": round(float(timings.get("predicted_per_second") or 0), 1),
            "prompt_tokens_per_sec": round(float(timings.get("prompt_per_second") or 0), 1),
            "context_tokens": CONTEXT_TOKENS,
            "finish": voice.finish_reason if voice and voice.finish_reason else finish,
        }
        audio = None
        if voice:
            stats.update(audio_seconds=voice.seconds(), first_audio_seconds=voice.first_audio)
            audio = voice.save(self.outputs / f"{job_id}.wav")
        memory = self.memory_gb()
        stats["peak_memory_gb"] = memory
        send(event="done", id=job_id, text=(text + markup).strip(), stats=stats, audio=audio, memory_gb=memory)

    def read_aloud(self, job):
        """Text to speech: the server asks with "Perform TTS in <language> using a <voice> voice."."""
        job_id, started = job["id"], time.time()
        system = next((str(m.get("content")) for m in job["messages"] if m.get("role") == "system"), "")
        user = next((m.get("content") for m in job["messages"] if m.get("role") == "user"), "")
        if isinstance(user, list):
            user = " ".join(p.get("text", "") for p in user if p.get("type") == "text")
        m = re.search(r"TTS in (.+?) using an? (\w+) voice", system)
        language, voice_name = (m.group(1), m.group(2)) if m else ("American English", "female")
        voice = Voice(self, job_id, stream=bool(job.get("stream")), started=started,
                      lang=LANG_CODES.get(language, language), voice=voice_name)
        sentences = Sentences()
        for piece in sentences.feed(str(user)) + sentences.flush():
            voice.say(piece)
        cancelled = self.is_cancelled(job_id)
        voice.finish(cancelled)
        elapsed = time.time() - started
        seconds = voice.seconds()
        frames = int(seconds / FRAME_SECONDS)
        audio = voice.save(self.outputs / f"{job_id}.wav")
        memory = self.memory_gb()
        stats = {"seconds": round(elapsed, 2), "prompt_tokens": 0, "context_tokens": CONTEXT_TOKENS, "tokens": frames,
                 "tokens_per_sec": round(frames / elapsed, 1) if elapsed else 0, "audio_seconds": seconds,
                 "first_audio_seconds": voice.first_audio, "peak_memory_gb": memory,
                 "finish": "cancelled" if cancelled() else (voice.finish_reason or "stop")}
        send(event="done", id=job_id, text=str(user).strip(), stats=stats, audio=audio, memory_gb=memory)


class Voice:
    """Speaks a reply's sentences one after another on its own thread, through /tts, while the
    reply is still being written. Streams the speech as audio_chunk events and keeps all of it."""

    PIECE = int(RATE * 0.25)

    def __init__(self, worker, job_id, stream, started, lang=None, voice=None):
        self.worker, self.job_id, self.stream, self.started = worker, job_id, stream, started
        self.lang, self.voice = lang, voice or os.environ.get("LFM3_VOICE", "female")
        self.queue, self.parts, self.first_audio, self.finish_reason = queue.Queue(), [], None, None
        self.dropped = False
        self.thread = threading.Thread(target=self._speak_all, daemon=True)
        self.thread.start()

    def say(self, text):
        words = speakable(text)
        if re.search(r"\w", words):
            self.queue.put(words)

    def finish(self, cancelled):
        self.queue.put(None)
        while self.thread.is_alive():
            if cancelled():
                self.abandon()
                self.finish_reason = "cancelled"
            self.thread.join(timeout=0.1)

    def abandon(self):
        self.dropped = True
        self.queue.put(None)

    def seconds(self):
        return round(sum(len(p) for p in self.parts) / RATE, 2)

    def save(self, path):
        if not self.parts:
            return None
        import numpy as np
        save_wav(path, np.concatenate(self.parts))
        return path.name

    def _speak_all(self):
        while (text := self.queue.get()) is not None and not self.dropped:
            try:
                self._speak(text)
            except Exception as exc:  # noqa: BLE001 - one sentence failing shouldn't end the reply
                log(f"Couldn't speak a sentence: {exc}")

    def _speak(self, text):
        import numpy as np
        lang = self.lang = self.lang or guess_lang(text)
        frames = int((0.8 * speakable_units(text) + 4) / FRAME_SECONDS)
        body = {"input": text, "voice": self.voice, "lang": lang, "stream": True, "response_format": "pcm",
                "n_predict": max(60, min(frames, 2048))}
        stream = Stream(self.worker.port, "/tts", body)
        watch, rest, pending = SpeechWatch(RATE, np), b"", []
        sentence = []

        def emit(samples):
            if self.stream and samples.size:
                send(event="audio_chunk", id=self.job_id, rate=RATE, pcm=base64.b64encode(samples.tobytes()).decode())
                self.first_audio = self.first_audio or round(time.time() - self.started, 2)

        while (item := stream.get(lambda: self.dropped)) is not None:
            kind, value, extra = item
            if kind == "status":
                raise error_of(value, extra)
            if kind == "failed":
                raise value
            if kind == "end":
                break
            if kind != "bytes":
                continue
            data = rest + value
            whole = len(data) // 4 * 4
            rest = data[whole:]
            floats = np.frombuffer(data[:whole], dtype="<f4")
            noise_from = watch.feed(floats)
            samples = pcm16(floats)
            sentence.append(samples)
            if noise_from is not None:
                # It lost its place: keep the speech before the noise and go on to the next sentence.
                stream.close()
                kept = np.concatenate(sentence)[:int(noise_from * RATE)]
                sentence, pending = [kept], []
                self.finish_reason = "noise"
                break
            pending.append(samples)
            if sum(len(p) for p in pending) >= self.PIECE:
                emit(np.concatenate(pending))
                pending = []
        if sentence and not self.dropped:
            # A breath between sentences, streamed too so what's heard matches the saved file.
            pending.append(np.zeros(int(RATE * 0.12), dtype="<i2"))
            sentence.append(pending[-1])
        if pending and not self.dropped:
            emit(np.concatenate(pending))
        if sentence:
            self.parts.append(np.concatenate(sentence))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--outputs", required=True)
    args = parser.parse_args()
    worker = Worker(args)
    send(event="loading", model=args.model)
    started = time.time()
    try:
        worker.start()
    except Exception as exc:  # noqa: BLE001
        log(traceback.format_exc())
        worker.stop()
        send(event="fatal", message=f"Could not load the model: {exc}")
        return 1
    load_seconds = round(time.time() - started, 1)
    log(f"Model loaded in {load_seconds} s")
    send(event="ready", load_seconds=load_seconds, memory_gb=worker.memory_gb())

    jobs = queue.Queue()

    def read_stdin():
        for raw in sys.stdin:
            raw = raw.strip()
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                log(f"Ignored a malformed line: {raw[:80]}")
                continue
            if message.get("cmd") == "cancel":
                worker.cancelled.add(message.get("id"))
            else:
                jobs.put(message)
        jobs.put(None)  # stdin closed: the app is gone, so exit

    threading.Thread(target=read_stdin, daemon=True).start()
    try:
        while (job := jobs.get()) is not None:
            try:
                worker.run(job)
            except PromptTooLong as exc:
                send(event="error", id=job.get("id"), code="context", prompt_tokens=exc.prompt_tokens,
                     context_tokens=CONTEXT_TOKENS, message=str(exc))
            except Exception as exc:  # noqa: BLE001
                log(traceback.format_exc())
                send(event="error", id=job.get("id"), message=f"Generation failed: {exc}")
            finally:
                worker.cancelled.discard(job.get("id"))
    finally:
        worker.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

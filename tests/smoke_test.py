"""End-to-end check of LFM3-Omni Lab against a running server.

Start the app first (./start.sh), then:  .venv/bin/python tests/smoke_test.py
It starts, uses, restarts and stops the model through the same API the page uses.
"""

import base64
import http.client
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HOST, PORT = "127.0.0.1", int(os.environ.get("PORT", 8765))
ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "static" / "samples"
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
results = []


# ---- HTTP helpers ------------------------------------------------------------
def request(method, path, body=None, headers=None, timeout=600):
    conn = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    conn.request(method, path, body=body, headers=headers or {})
    return conn, conn.getresponse()


def call(method, path, payload=None):
    body = json.dumps(payload).encode() if payload is not None else None
    conn, resp = request(method, path, body, {"Content-Type": "application/json"} if body else {})
    data = json.loads(resp.read() or b"{}")
    conn.close()
    return resp.status, data


def upload(path, voice=False):
    boundary = uuid.uuid4().hex
    body = bytearray()
    if voice:
        body += f'--{boundary}\r\nContent-Disposition: form-data; name="voice"\r\n\r\n1\r\n'.encode()
    body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{Path(path).name}"\r\n'
             f"Content-Type: application/octet-stream\r\n\r\n").encode()
    body += Path(path).read_bytes() + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    conn, resp = request("POST", "/api/media", bytes(body), {"Content-Type": f"multipart/form-data; boundary={boundary}"})
    data = json.loads(resp.read())
    conn.close()
    return resp.status, data


def chat(text="", media=(), history=(), reply="auto", tools=True, on_event=None, **extra):
    """POST /api/chat and collect the streamed events. Returns (http_status, events)."""
    payload = {"history": list(history), "message": {"text": text, "media": list(media)},
               "reply": reply, "tools": tools, **extra}
    conn, resp = request("POST", "/api/chat", json.dumps(payload).encode(), {"Content-Type": "application/json"})
    if resp.status != 200:
        data = json.loads(resp.read())
        conn.close()
        return resp.status, [data]
    events, t0 = [], time.time()
    while line := resp.readline():
        event = json.loads(line)
        event["_at"] = time.time() - t0  # seconds after the request, for first-audio timing
        events.append(event)
        if on_event:
            on_event(event)
    conn.close()
    return 200, events


def streamed_seconds(events):
    return sum(len(base64.b64decode(e["pcm"])) / 2 / e["rate"] for e in events if e.get("event") == "audio")


def final(events):
    return next((e for e in reversed(events) if e.get("event") in ("done", "error")), {})


def served_wav(name):
    if not name:
        return False
    conn, resp = request("GET", f"/outputs/{name}")
    data = resp.read()
    conn.close()
    return resp.status == 200 and data[:4] == b"RIFF" and len(data) > 10_000


def check(name, ok, detail=""):
    results.append((name, bool(ok)))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"\n        {detail}" if detail else ""), flush=True)


def wait_for(state, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        _, status = call("GET", "/api/model")
        if status["state"] == state or (status["state"] == "error" and state != "error"):
            return status
        time.sleep(0.5)
    return call("GET", "/api/model")[1]


def pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def turn(name, text, media=(), history=(), must_contain=(), tools_used=None, spoken=False, **options):
    """One conversation turn with checks. Returns the history including this turn."""
    t = time.time()
    status, events = chat(text, media, history, **options)
    done = final(events)
    answer = done.get("text", "")
    called = [e["name"] for e in events if e.get("event") == "tool_call"]
    started = events[0] if events else {}
    audio_seconds = (done.get("stats") or {}).get("audio_seconds") or 0
    problems = []
    if status != 200 or done.get("event") != "done":
        problems.append(f"status={status} final={done}")
    missing = [w for w in must_contain if w.lower() not in answer.lower()]
    if missing:
        problems.append(f"missing {missing}")
    if tools_used is not None and called != list(tools_used):
        problems.append(f"tools called {called}, expected {list(tools_used)}")
    streamed = streamed_seconds(events)
    first_audio = next((e["_at"] for e in events if e.get("event") == "audio"), None)
    if spoken and not (started.get("speak") and served_wav(done.get("audio")) and audio_seconds >= 1):
        problems.append(f"expected a spoken reply, got audio={done.get('audio')} ({audio_seconds}s)")
    if spoken and abs(streamed - audio_seconds) > 0.25:
        problems.append(f"streamed {streamed:.2f}s of speech but the file has {audio_seconds}s")
    if not spoken and done.get("audio"):
        problems.append("expected a text reply, got speech")
    tokens = sum(1 for e in events if e.get("event") == "token")
    detail = (f"{time.time() - t:.1f}s, {tokens} streamed chunks, tools={called}"
              + (f", {audio_seconds}s of speech, first audio at {first_audio:.1f}s" if done.get("audio") and first_audio else "")
              + f"\n        -> {answer[:220]!r}")
    check(name, not problems, "; ".join(problems) + "\n        " + detail if problems else detail)
    return list(history) + done.get("append", []), done, events


def main():
    tmp = Path(tempfile.mkdtemp(prefix="lfm3-smoke-"))
    print(f"Testing http://{HOST}:{PORT}\n")

    # ---- page and options
    conn, resp = request("GET", "/")
    html = resp.read().decode()
    conn.close()
    check("page loads", resp.status == 200 and 'id="composer"' in html)
    for asset in ("/app.js", "/style.css", "/samples/sign.png", "/samples/speech.wav", "/samples/clip.mp4"):
        conn, resp = request("GET", asset)
        resp.read()
        conn.close()
        check(f"serves {asset}", resp.status == 200)
    _, options = call("GET", "/api/options")
    check("options list the tools and voices",
          [(t["name"], t["group"]) for t in options["tools"]] == [
              ("get_current_time", "local"), ("calculate", "local"), ("add_reminder", "local"), ("list_reminders", "local"),
              ("web_search", "web"), ("open_page", "web"), ("click", "web"), ("type_text", "web"), ("go_back", "web")]
          and "Arabic" in options["tts_languages"] and options["default_system"])
    for asset in ("/orb.js",):
        conn, resp = request("GET", asset)
        resp.read()
        conn.close()
        check(f"serves {asset}", resp.status == 200)

    # ---- the web browser's address guard (no model needed)
    sys.path.insert(0, str(ROOT))
    from app.browser import is_local
    blocked = ["localhost", "127.0.0.1", "::1", "192.168.1.20", "10.0.0.5", "172.16.3.4", "169.254.169.254",
               "printer.local", "nas.lan", "0.0.0.0"]
    allowed = ["example.com", "en.wikipedia.org", "8.8.8.8"]
    check("web: addresses on this Mac and the local network are refused",
          all(is_local(h) for h in blocked) and not any(is_local(h) for h in allowed),
          [h for h in blocked if not is_local(h)] + [h for h in allowed if is_local(h)])

    # ---- guards around generation (no model needed)
    from app.guards import CONTEXT_TOKENS, PromptTooLong, SpeechWatch, reply_budget

    def noise_from(path):
        """Feed a recording to the speech guard in the worker's piece size; where it calls noise."""
        raw = subprocess.run([FFMPEG, "-loglevel", "error", "-i", str(path), "-f", "s16le", "-ac", "1", "-ar", "24000", "-"],
                             capture_output=True).stdout
        import numpy as np
        audio, watch = np.frombuffer(raw, "<i2").astype(np.float32) / 32768, SpeechWatch(24000)
        for i in range(0, len(audio), 11520):
            if (hit := watch.feed(audio[i:i + 11520])) is not None:
                return hit
        return None
    fixtures = Path(__file__).parent / "fixtures"
    # A real reply that lost its place: clear speech, then from 18 s on, rumble until the token limit.
    hit = noise_from(fixtures / "speech_turns_to_noise.opus")
    check("speech guard: finds where speech turned into noise", hit is not None and 16 <= hit <= 20, hit)
    hit = noise_from(fixtures / "speech_clean.opus")
    check("speech guard: leaves clean speech alone", hit is None, hit)
    try:
        reply_budget(CONTEXT_TOKENS - 10, 1024)
        refused = False
    except PromptTooLong:
        refused = True
    check("context guard: a reply gets what's left of the 32K context, and a full prompt is refused",
          reply_budget(30000, 4096) == CONTEXT_TOKENS - 30000 and reply_budget(500, 1024) == 1024 and refused)

    # ---- which checkpoint start.sh picks (no download)
    from app import pick_model
    import contextlib, io

    def picks(memory, installed):
        pick_model.memory_gb = lambda: memory
        pick_model.local_copy = lambda repo: repo if repo.rsplit("-", 1)[1] in installed else None
        fetched = []
        pick_model.download = lambda repo, size=None: fetched.append(repo.rsplit("-", 1)[1])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            pick_model.main()
        return out.getvalue().strip().rsplit("-", 1)[1], fetched
    os.environ.pop("LFM3_MODEL", None)
    cases = {(8, ()): ("4bit", ["4bit"]), (8, ("8bit",)): ("4bit", ["4bit"]), (16, ()): ("8bit", ["8bit"]),
             (16, ("4bit",)): ("4bit", []), (24, ("8bit",)): ("8bit", []), (36, ("bf16",)): ("bf16", [])}
    got = {k: picks(*k) for k in cases}
    wrong = {k: v for k, v in got.items() if v != cases[k]}
    check("start.sh: picks 4-bit under 16 GB, else 8-bit, and reuses a download that fits", not wrong, wrong)

    # ---- stopped
    _, status = call("GET", "/api/model")
    if status["state"] != "stopped":
        call("POST", "/api/model/stop")
    status = wait_for("stopped", 30)
    check("model starts stopped", status["state"] == "stopped")
    code, events = chat("hi")
    check("chat is refused while stopped", code == 409, events[0].get("error", ""))
    code, data = call("POST", "/api/speak", {"text": "hi"})
    check("read aloud is refused while stopped", code == 409, data.get("error"))

    # ---- attachments (prepared without the model)
    code, sign = upload(SAMPLES / "sign.png")
    check("upload: PNG image (works while the model is stopped)", code == 200 and sign["kind"] == "image", sign)
    heic = tmp / "photo.heic"
    subprocess.run(["sips", "-s", "format", "heic", str(SAMPLES / "sign.png"), "--out", str(heic)], capture_output=True)
    code, heic_item = upload(heic) if heic.exists() else (0, {})
    check("upload: iPhone HEIC photo", code == 200 and heic_item.get("kind") == "image", heic_item)
    code, speech = upload(SAMPLES / "speech.wav")
    check("upload: WAV recording", code == 200 and speech["kind"] == "audio" and speech["seconds"] > 3, speech)
    webm = tmp / "recording.webm"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(SAMPLES / "speech.wav"), "-c:a", "libopus", str(webm)])
    code, webm_item = upload(webm)
    check("upload: browser recording (webm/opus)", code == 200 and webm_item["kind"] == "audio", webm_item)
    m4a = tmp / "memo.m4a"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(SAMPLES / "speech.wav"), "-c:a", "aac", str(m4a)])
    code, m4a_item = upload(m4a)
    check("upload: Safari/iPhone recording (m4a)", code == 200 and m4a_item["kind"] == "audio", m4a_item)
    code, clip = upload(SAMPLES / "clip.mp4")
    check("upload: video with sound", code == 200 and clip["kind"] == "video" and clip["soundtrack"] is True, clip)
    silent = tmp / "silent.mp4"
    subprocess.run([FFMPEG, "-y", "-loglevel", "error", "-i", str(SAMPLES / "clip.mp4"), "-an", "-c:v", "copy", str(silent)])
    code, silent_clip = upload(silent)
    check("upload: video without sound", code == 200 and silent_clip["soundtrack"] is False, silent_clip)
    bogus = tmp / "not-audio.wav"
    bogus.write_text("this is not audio")
    code, data = upload(bogus)
    check("upload: unreadable audio gives a clear error", code == 400 and "read" in data.get("error", "").lower(), data.get("error"))
    notes = tmp / "notes.txt"
    notes.write_text("hello")
    code, data = upload(notes)
    check("upload: unsupported file type is refused", code == 400 and "image" in data.get("error", ""), data.get("error"))

    # Spoken questions, made with macOS text to speech (AIFF also tests another audio format)
    spoken = {}
    for key, line, voice in [("moon", "Tell me one fun fact about the moon.", None), ("time", "What time is it in Tokyo right now?", None),
                             # Fred's voice is one the model followed less than its own earlier answers
                             ("story", "Tell me a story.", "Fred")]:
        aiff = tmp / f"{key}.aiff"
        subprocess.run(["say", *(["-v", voice] if voice else []), "-o", str(aiff), line], capture_output=True)
        code, item = upload(aiff, voice=True)
        spoken[key] = item if code == 200 else None
    check("upload: spoken questions as voice messages", all(v and v["voice"] for v in spoken.values()), spoken)
    # a second picture, nothing like the sign
    from PIL import Image, ImageDraw
    circle_png = tmp / "circle.png"
    picture = Image.new("RGB", (640, 400), "white")
    ImageDraw.Draw(picture).ellipse((220, 100, 420, 300), fill=(220, 30, 30))
    picture.save(circle_png)
    code, circle = upload(circle_png)
    check("upload: a second image", code == 200 and circle["kind"] == "image", circle)

    # ---- start
    t = time.time()
    call("POST", "/api/model/start")
    _, mid = call("GET", "/api/model")
    status = wait_for("ready")
    check("Start loads the model", status["state"] == "ready" and status["pid"],
          f"state seen while loading: {mid['state']}; ready in {time.time() - t:.1f}s; load {status['load_seconds']}s; "
          f"MLX memory {status['memory_gb']} GB")
    first_pid = status["pid"]

    # ---- conversation
    h, _, _ = turn("text: single question, no tool needed", "Name a famous bridge in San Francisco.",
                   must_contain=["Golden Gate"], tools_used=[])
    turn("text: remembers earlier turns", "What year did it open? Answer with just the year.", history=h, must_contain=["1937"])
    turn("text: custom system prompt is followed", "What color is the sky on a clear day?",
         system="Always answer in French.", must_contain=["bleu"])
    code, events = chat("")
    check("text: empty message is refused", code == 400, events[0].get("error"))
    code, events = chat("what is this?", media=["doesnotexist"])
    check("text: a missing attachment gives a clear error", code == 400 and "no longer available" in events[0].get("error", ""),
          events[0].get("error"))

    # ---- tools
    h, done, events = turn("tools: clock", "What time is it in Tokyo right now?", tools_used=["get_current_time"])
    call_event = next((e for e in events if e.get("event") == "tool_call"), {})
    check("tools: clock was asked for Tokyo's zone", call_event.get("arguments", {}).get("timezone") == "Asia/Tokyo", call_event)
    check("tools: the call and result join the history", [m["role"] for m in h] == ["user", "assistant", "tool", "assistant"]
          and h[1].get("tool_calls"), [m["role"] for m in h])
    turn("tools: calculator", "What's 17.5% of 2,340?", must_contain=["409.5"], tools_used=["calculate"])
    call("DELETE", "/api/reminders")
    turn("tools: save a reminder", "Remind me to buy milk tomorrow morning.", tools_used=["add_reminder"])
    _, saved = call("GET", "/api/reminders")
    check("tools: the reminder is stored", saved["count"] == 1 and "milk" in saved["reminders"][0]["text"].lower(), saved)
    turn("tools: list reminders", "What reminders do I have?", must_contain=["milk"], tools_used=["list_reminders"])
    turn("tools: switched off, nothing is called", "What time is it in Tokyo right now?", tools=False, tools_used=[])
    call("DELETE", "/api/reminders")
    check("tools: reminders can be cleared", call("GET", "/api/reminders")[1]["count"] == 0)

    # ---- images, audio and video in the conversation
    h, _, _ = turn("image: reads the sign", "What does the sign say?", media=[sign["id"]], must_contain=["CAFE", "OPEN"])
    h, _, _ = turn("image: follow-up about the same picture", "What color is the triangle in that picture?",
                   history=h, must_contain=["green"])
    h, _, _ = turn("audio: transcribes a recording (text reply, not a voice message)", "Transcribe this recording word for word.",
                   media=[speech["id"]], history=h, must_contain=["electrician", "Thursday"], tools_used=[])
    turn("context: earlier image still known after an audio turn", "What did the sign in the first picture say?",
         history=h, must_contain=["CAFE"])
    # With the sign still in view, "What's in this picture?" about a new photo described the sign.
    _, done, _ = turn("image: a new picture is the one described", "What's in this picture?", media=[circle["id"]],
                      history=h, must_contain=["circle"])
    check("image: ...not the earlier sign", "cafe" not in done.get("text", "").lower(), done.get("text", "")[:160])
    if heic_item:
        turn("image: HEIC photo", "What does the sign say?", media=[heic_item["id"]], must_contain=["CAFE"])
    turn("audio: webm recording", "Transcribe this recording word for word.", media=[webm_item["id"]],
         must_contain=["electrician"], tools_used=[])
    turn("audio: m4a recording", "Transcribe this recording word for word.", media=[m4a_item["id"]],
         must_contain=["electrician"], tools_used=[])
    turn("audio: translates speech and stays in text", "Translate what is said into French.",
         media=[speech["id"]], must_contain=["lectricien"], tools_used=[])
    turn("audio: summarizing a recording doesn't act on it", "Summarize this recording.",
         media=[speech["id"]], must_contain=["electrician"], tools_used=[])
    turn("video: frames and soundtrack", "What text appears on screen, and what does the narrator say?",
         media=[clip["id"]], must_contain=["CAFE", "welcome"])
    turn("video: soundtrack is heard with tools on", "Describe what happens in this clip and what you can hear.",
         media=[clip["id"]], must_contain=["welcome"])
    turn("video: without sound", "What text appears on screen?", media=[silent_clip["id"]], must_contain=["CAFE"])

    # ---- spoken replies
    code, voice_note = upload(SAMPLES / "speech.wav", voice=True)
    h, _, events = turn("voice: voice message gets a spoken reply and uses a tool", "", media=[voice_note["id"]],
                        tools_used=["add_reminder"], spoken=True)
    first_audio = next((e["_at"] for e in events if e.get("event") == "audio"), 99)
    tool_at = next((e["_at"] for e in events if e.get("event") == "tool_result"), 0)
    check("voice: speech starts streaming soon after the tool", first_audio - tool_at < 2.0,
          f"tool result at {tool_at:.1f}s, first audio at {first_audio:.1f}s")
    said = next((e for e in events if e.get("event") == "transcript"), {})
    first_out = next((e["_at"] for e in events if e.get("event") in ("token", "audio", "tool_call")), 99)
    check("voice: the voice message is written down before it's answered",
          said.get("media") == voice_note["id"] and "electrician" in said.get("text", "").lower() and said.get("_at", 99) < first_out,
          f"{said.get('text')!r} at {said.get('_at', 0):.1f}s, answer starts at {first_out:.1f}s")
    turn("voice: a later turn knows what the voice message said", "Who did I say was coming, and on which day?",
         history=h, must_contain=["electrician", "Thursday"], tools_used=[])
    # The user's conversation: it described itself, then gave the same description to something it
    # couldn't make out, and then to a clear "Tell me a story". The transcript beside the audio fixed it.
    identity = ("I am LFM3-Omni-2.2B, a Liquid Foundation Model with about 2.2 billion parameters, created by Liquid AI. "
                "I can take text, images, audio, and video as input and produce text and speech as output.")
    alike = [{"role": "user", "text": "Hello.", "media": []},
             {"role": "assistant", "content": "Hello! What can I help you with today?"},
             {"role": "user", "text": "Tell me about yourself.", "media": []},
             {"role": "assistant", "content": identity},
             {"role": "user", "text": "Suga for the song.", "media": []},
             {"role": "assistant", "content": identity + " Liquid AI focuses on building capable, efficient foundation models for on-device intelligence."}]
    _, done, _ = turn("voice: after two answers alike, a spoken request gets its own answer", "", media=[spoken["story"]["id"]],
                      history=alike, web=True, tools_used=[], spoken=True)
    check("voice: ...a story, not the same description again", "liquid ai" not in done.get("text", "").lower(), done.get("text", "")[:160])
    turn("voice: spoken question without a tool", "", media=[spoken["moon"]["id"]], must_contain=["moon"],
         tools_used=[], spoken=True)
    _, _, events = turn("voice: spoken question that needs a tool", "", media=[spoken["time"]["id"]],
                        tools_used=["get_current_time"], spoken=True)
    said = next((e.get("text", "") for e in events if e.get("event") == "transcript"), "")
    check("voice: a spoken question is written down, not answered", "what time" in said.lower(), said)
    # A live-mode turn boosted by the browser's automatic gain until it peaked at full scale.
    # At that level the model answered it ("I don't know the current time in Tokyo") instead of
    # writing it down, until the server started turning loud audio down.
    # The recording is a real person's voice, so it isn't published; a synthetic voice at the
    # same level doesn't trip the model. Without the file this check is skipped.
    fixture = Path(__file__).parent / "fixtures" / "loud_live_turn.wav"
    if fixture.exists():
        code, loud = upload(fixture, voice=True)
        _, _, events = turn("voice: a very loud live-mode recording", "", media=[loud["id"]],
                            tools_used=["get_current_time"], spoken=True)
        said = next((e.get("text", "") for e in events if e.get("event") == "transcript"), "")
        check("voice: a very loud recording is still written down word for word",
              "what time is it in tokyo" in said.lower(), said)
    else:
        print("[SKIP] voice: a very loud live-mode recording (tests/fixtures/loud_live_turn.wav isn't here)")
    turn("voice: Voice reply to a typed message", "Say hello in one short sentence.", reply="voice", tools_used=[], spoken=True)
    # With tools on, a spoken reply starts in "auto", where the model may start writing instead.
    # It's stopped at its first token and asked to speak, rather than writing the whole answer first.
    since = call("GET", "/api/logs?since=0")[1]["last"]
    _, done, events = turn("voice: a reply it starts to write is asked to speak at once", "Hi, how are you?",
                           reply="voice", tools_used=[], spoken=True)
    lines = [entry["line"] for entry in call("GET", f"/api/logs?since={since}")[1]["lines"]]
    first_audio = next((e["_at"] for e in events if e.get("event") == "audio"), 99)
    check("voice: ...and it speaks within 3 seconds", any("started a written answer" in l for l in lines) and first_audio < 3,
          f"first audio at {first_audio:.1f}s; " + " / ".join(l for l in lines if "reply" in l)[:300])
    turn("voice: reads a sentence aloud without calling tools",
         "Read this aloud exactly: Everything you hear is generated on this Mac, with no internet connection.",
         reply="voice", must_contain=["generated on this Mac"], tools_used=[], spoken=True)
    turn("voice: typed arithmetic with a spoken reply", "What's 17.5% of 2,340?", reply="voice",
         must_contain=["409"], tools_used=["calculate"], spoken=True)
    turn("voice: Text reply to a voice message", "", media=[spoken["moon"]["id"]], reply="text", must_contain=["moon"])
    turn("voice: tools off still speaks", "", media=[spoken["moon"]["id"]], tools=False, spoken=True)
    call("DELETE", "/api/reminders")

    # ---- read aloud
    for name, payload, language in [
        ("read aloud: American English, female", {"text": "Good morning. This voice was generated on a Mac.",
                                                   "language": "American English", "voice": "female"}, "American English"),
        ("read aloud: British English, male", {"text": "The quick brown fox jumps over the lazy dog.",
                                                "language": "British English", "voice": "male"}, "British English"),
        ("read aloud: Arabic is detected automatically", {"text": "مرحبا، كيف حالك اليوم؟", "language": "auto"}, "Arabic"),
        ("read aloud: markdown is read as plain text", {"text": "**Note:** the `cafe` opens at *8am*."}, "American English"),
    ]:
        code, data = call("POST", "/api/speak", payload)
        check(name, code == 200 and data.get("language") == language and served_wav(data.get("audio")),
              f"{data.get('language')}, {(data.get('stats') or {}).get('audio_seconds')}s" if code == 200 else data)
    code, data = call("POST", "/api/speak", {"text": "hi", "language": "Klingon"})
    check("read aloud: unsupported language is refused", code == 400, data.get("error"))
    code, data = call("POST", "/api/speak", {"text": "  "})
    check("read aloud: empty text is refused", code == 400, data.get("error"))

    # ---- stop an answer
    job = {}

    def cancel_after_some(event):
        if event.get("event") == "started":
            job["id"] = event["id"]
        if event.get("event") == "token":
            job["n"] = job.get("n", 0) + 1
            if job["n"] == 15:
                call("POST", f"/api/cancel/{job['id']}")

    _, events = chat("Write a 600-word essay about the history of bridges.", max_tokens=2000, on_event=cancel_after_some)
    done = final(events)
    check("Stop ends the answer early and keeps what was written",
          (done.get("stats") or {}).get("finish") == "cancelled" and done.get("append", [{}])[-1].get("content"),
          f"stopped after {(done.get('stats') or {}).get('tokens')} tokens")

    spoken_job = {}

    def cancel_on_speech(event):
        if event.get("event") == "started":
            spoken_job["id"] = event["id"]
        if event.get("event") == "audio" and not spoken_job.get("sent"):
            spoken_job["sent"] = time.time()
            call("POST", f"/api/cancel/{spoken_job['id']}")

    _, events = chat("Tell me a long story about a lighthouse keeper.", reply="voice", max_tokens=2000, on_event=cancel_on_speech)
    done = final(events)
    check("Stop ends a spoken reply while it streams",
          (done.get("stats") or {}).get("finish") == "cancelled" and streamed_seconds(events) < 15
          and time.time() - spoken_job.get("sent", 0) < 30,
          f"{streamed_seconds(events):.1f}s of speech streamed before it stopped")

    # Stopped before the answer starts, while the voice message is still being written down
    aiff = tmp / "stop_early.aiff"
    subprocess.run(["say", "-o", str(aiff), "Tell me a long story about a lighthouse keeper."], capture_output=True)
    _, early = upload(aiff, voice=True)
    _, events = chat("", media=[early["id"]], reply="voice", max_tokens=2000,
                     on_event=lambda e: e.get("event") == "started" and call("POST", f"/api/cancel/{e['id']}"))
    done = final(events)
    check("Stop works while a voice message is being written down",
          (done.get("stats") or {}).get("finish") == "cancelled" and streamed_seconds(events) == 0
          and not any(e.get("event") == "token" for e in events), [e.get("event") for e in events])

    # ---- the 32K context
    filler = "The quick brown fox jumps over the lazy dog near the riverbank while seven tailors stitch a kandura. " * 60
    long_history = []
    for i in range(26):  # about 36,000 tokens
        long_history += [{"role": "user", "text": f"Note {i}: {filler}", "media": []}, {"role": "assistant", "content": f"Noted {i}."}]
    _, events = chat("How many notes did I send? Answer with a number.", history=long_history, reply="text", tools=False)
    trimmed, done = [e for e in events if e.get("event") == "trimmed"], final(events)
    prompt = (done.get("stats") or {}).get("prompt_tokens", 0)
    check("context: a conversation past 32K leaves out its earliest turns, once, and answers with room to spare",
          len(trimmed) == 1 and done.get("event") == "done" and prompt + 1024 <= 32768,
          f"{trimmed}, prompt {prompt}, {done.get('event')}")
    _, events = chat("Repeat this: " + filler * 25, reply="text", tools=False)
    done = final(events)
    check("context: a single message too long for 32K is refused with the reason",
          done.get("event") == "error" and "32K" in done.get("message", ""), done.get("message"))

    # ---- web tools (needs an internet connection)
    web_events = []
    h, done, events = turn("web: searches when Web is on", "Search the web for the year the Golden Gate Bridge opened.",
                           web=True, must_contain=["1937"])
    web_events += events
    called = [e["name"] for e in events if e.get("event") == "tool_call"]
    check("web: used web_search", "web_search" in called, called)
    result = next((e for e in events if e.get("event") == "tool_result" and e["name"] == "web_search"), {})
    check("web: search results reach the model as titles, addresses and snippets",
          (result.get("result") or {}).get("results") and "_screenshot" not in result.get("result", {}),
          [r.get("title") for r in (result.get("result") or {}).get("results", [])][:3])
    h, done, events = turn("web: opens a page", "Open https://example.com and tell me the page's heading.",
                           web=True, must_contain=["Example Domain"], tools_used=["open_page"])
    web_events += events
    turn("web: follows a link on the open page", "Follow link 1 on that page and tell me the new page's title.",
         history=h, web=True, tools_used=["click"])
    h, done, events = turn("web: types into a page's search box and submits it",
                           "Open https://en.wikipedia.org/wiki/Main_Page, search it for the Eiffel Tower with the page's "
                           "search box, and tell me how tall the tower is.",
                           web=True, tools_used=["open_page", "type_text"], must_contain=["330"])
    web_events += events
    typed = next((e.get("result") or {} for e in events if e.get("event") == "tool_result" and e["name"] == "type_text"), {})
    check("web: typing reports what it typed into and lands on the result", "search box" in str(typed.get("typed_into"))
          and "Eiffel" in str(typed.get("title")), f"{typed.get('typed_into')} -> {typed.get('title')}")
    turn("web: goes back a page", "Go back to the previous page and tell me its title.", history=h, web=True,
         tools_used=["go_back"], must_contain=["Wikipedia"])
    # The model sometimes declines a local address by itself; when it does try, the browser must refuse.
    tried, opened = [], []
    for address in ("http://localhost:8765", "http://127.0.0.1:8765", "http://192.168.1.1"):
        events = chat(f"Open {address} and tell me what it says.", web=True)[1]
        for e in events:
            if e.get("event") == "tool_result" and e["name"] in ("open_page", "click"):
                error = str((e.get("result") or {}).get("error", ""))
                (tried if "can't be opened" in error else opened).append(f"{address}: {error or e['result'].get('title')}")
    check("web: a local address is never opened, and the refusal says why", tried and not opened,
          f"refused: {tried}; opened: {opened}")
    shots = [e["screenshot"] for e in web_events if e.get("event") == "tool_result" and e.get("screenshot")]
    ok = bool(shots)
    for shot in shots:
        conn, resp = request("GET", shot)
        data = resp.read()
        conn.close()
        ok = ok and resp.status == 200 and data[:3] == b"\xff\xd8\xff"
    check("web: each step saves a screenshot the page can show", ok, f"{len(shots)} screenshots")
    # Read out, a page of results ran to a minute of speech; a note beside them asks for a summary.
    _, done, events = turn("web: a spoken answer after a search", "Search the web for good books on machine learning.",
                           web=True, reply="voice", spoken=True)
    words = len(done.get("text", "").split())
    check("web: ...sums up the results instead of reading them out", "web_search" in [e.get("name") for e in events if e.get("event") == "tool_call"]
          and words <= 80 and (done.get("stats") or {}).get("finish") == "stop", f"{words} words, finish {(done.get('stats') or {}).get('finish')}")
    turn("web: offered only when Web is on", "Search the web for the year the Golden Gate Bridge opened.",
         web=False, tools_used=[])
    turn("web: Tools off turns the web off too", "Search the web for the year the Golden Gate Bridge opened.",
         tools=False, web=True, tools_used=[])

    # ---- restart
    t = time.time()
    call("POST", "/api/model/restart")
    status = wait_for("ready")
    check("Restart reloads the model in a new process",
          status["state"] == "ready" and status["pid"] != first_pid and not pid_alive(first_pid),
          f"{time.time() - t:.1f}s, pid {first_pid} -> {status['pid']}")
    turn("attachments survive a model restart", "What does the sign say?", media=[sign["id"]], must_contain=["CAFE"])

    # ---- stop the model during an answer
    second_pid = status["pid"]
    seen = {}

    def stop_mid_answer(event):
        if event.get("event") == "token" and not seen:
            seen["stopped"] = True
            call("POST", "/api/model/stop")

    _, events = chat("Write a long story about a lighthouse.", max_tokens=2000, on_event=stop_mid_answer)
    status = wait_for("stopped", 30)
    check("Stopping the model mid-answer ends it cleanly", final(events).get("event") == "error" and status["state"] == "stopped",
          final(events).get("message"))
    check("Stop frees the model's memory (process is gone)", not pid_alive(second_pid) and status["memory_gb"] is None)

    # ---- crash recovery
    call("POST", "/api/model/start")
    status = wait_for("ready")
    os.kill(status["pid"], signal.SIGKILL)
    status = wait_for("error", 15)
    check("a crashed model is reported", status["state"] == "error", status.get("error"))
    call("POST", "/api/model/start")
    status = wait_for("ready")
    check("the model starts again after a crash", status["state"] == "ready")
    call("POST", "/api/model/stop")
    check("final stop", wait_for("stopped", 30)["state"] == "stopped")

    shutil.rmtree(tmp, ignore_errors=True)
    failed = [n for n, ok in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    if failed:
        print("Failed:\n  " + "\n  ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

# LFM3-Omni Lab

One conversation page for [LFM3-Omni-2B](https://huggingface.co/LiquidAI/LFM3-Omni-2B) on an Apple Silicon Mac.

- Type to it, talk to it, or attach images, recordings and videos.
- It answers in writing or out loud.
- It can call a few tools on your Mac, and it can search and read the web when you allow it.
- Live mode lets you hold a spoken conversation without touching anything.

The model runs on your Mac with MLX. Nothing leaves the machine after the one-time model download, unless you turn on Web.

## Start

```bash
cd ~/projects/lfm3-omni-lab
./start.sh
```

It needs a Mac with Apple Silicon (M1 or later) and Python 3.10 or newer. macOS's own `python3` is 3.9, so the script looks for a newer one (`brew install python` if there isn't one).

The first run creates `.venv` and installs `mlx-vlm` and Playwright, which takes a minute or two. Then the script picks the model checkpoint for this Mac and downloads it if it isn't already here:

| This Mac | Checkpoint | Download |
|---|---|---|
| Less than 16 GB of memory | `LiquidAI/LFM3-Omni-2B-MLX-4bit` | 2.1 GB |
| 16 GB or more | `LiquidAI/LFM3-Omni-2B-MLX-8bit`, the one this app is tested with | 2.9 GB |

A checkpoint that's already in the Hugging Face cache and fits the Mac is used as it is, so nobody downloads a second copy. The bf16 checkpoint (4.5 GB) is used only when it's already there or you ask for it: on a base M-series chip it can fall behind real time while speaking, and speech then stutters. A download that was cut off resumes on the next run. The logic is in `app/pick_model.py`.

The page opens at http://localhost:8765.

- **Start model** loads the model in a few seconds.
- **Stop** unloads the model and frees its memory.
- **Restart** reloads it in a fresh process.
- Ctrl+C in the terminal quits the app and stops the model.

Audio, recording and video need `ffmpeg` (`brew install ffmpeg`). Web needs Google Chrome, or Playwright's own Chromium (`.venv/bin/playwright install chromium`).

| Setting | How |
|---|---|
| Another port | `PORT=8766 ./start.sh` |
| Another checkpoint (e.g. bf16, or a local folder) | `LFM3_MODEL=LiquidAI/LFM3-Omni-2B-MLX-bf16 ./start.sh` |
| Load the model as soon as the app starts | `LFM3_AUTOSTART=1 ./start.sh` |
| Don't open a browser tab | `NO_BROWSER=1 ./start.sh` |
| Watch the web browser work in a visible window | `LFM3_BROWSER_HEADED=1 ./start.sh` |

## Using it

- **Type** a message and press Enter. Shift+Enter adds a line. Written replies stream in as they're written.
- **Attach** images (JPEG, PNG, HEIC…), recordings (WAV, MP3, M4A, WebM…) or videos.
  - You can drag files onto the page or paste images into the message box.
  - Attachments are prepared as soon as you add them, and suggestions appear for what to ask.
  - Every attachment stays in the conversation, so you can ask follow-up questions about it later.
- **Record** a voice message with the microphone in the message bar. Click once to start and again to send it. After the reply, the page shows what it heard under your message.
- **Reply** sets how the model answers:
  - **Auto** speaks back when you send a voice message and writes back otherwise.
  - **Text** always writes.
  - **Voice** always speaks. Speech starts playing within about a second, while the rest is still being made.
- **Stop** (or Esc) ends a reply right away, written or spoken, and silences it.
- **Read aloud** under any written reply speaks it with the text-to-speech voice.
  - Choose female or male, and a language, in Settings. "Match the text" detects Arabic, Hebrew, Greek, Chinese, Japanese and Korean.
  - Spoken replies use the model's own conversational voice, which is always the same female voice.
- **Settings** holds the system prompt, temperature, reply length, read-aloud voice and your saved reminders. They're remembered in the browser.
- **Log** at the top opens the model log in a panel beside the conversation. It shows loading, any errors, and one line per reply: its prompt size against the 32K context, tokens made and how fast, seconds of speech, time to the first sound, peak memory and how it ended. Esc or Close hides it.

While the model works, the message bar glows: blue while it thinks, amber while it searches the web, green while it speaks, red while you record. The small orb at the end of the bar changes shape with it. Photos, videos and the pages the browser saw sit on dark cards.

Videos are trimmed to the first 30 seconds and scaled to 640 px on the long side to keep answers quick.

### Live conversation

Click the orb at the end of the message bar, or **Start a live conversation** on the empty page, to talk hands-free. Live mode fills the window.

1. Talk normally. A voice detector in the page waits for you to stop: it ends your turn after 0.8 seconds of quiet.
2. Your words go to the model as a voice message, and it answers out loud.
3. When it finishes speaking, it listens again.

While it works, a sphere of particles shows what it's doing:

- grey and rippling with your voice while it listens;
- a blue knot while it thinks;
- amber rings while it searches the web;
- a green sphere while it speaks, pulsing with the speech.

What you said appears in large type at the top once it's written down, with the tool it used underneath. Its words appear under the sphere as captions, and the particles fly into each word as it's spoken. When it opens a web page, a card with the page's screenshot appears beside the sphere. The edge of the window glows in the same colours as the sphere, brighter as you or it speaks.

Under the header, a row of figures shows how the last reply went:

| Figure | What it measures |
|---|---|
| Answered in | From the moment you stopped talking to its first sound. The small line beside it plots this for every reply so far. |
| Speech | Seconds of speech, and how many times faster than real time it was made once it started playing. Under 1.15× it turns amber: playback can catch up and stutter. |
| Speed | Tokens per second, counting text and speech frames |
| Clock, Web search… | Time spent in each tool |
| Context | The prompt's size out of the model's 32K context, as a meter. It turns amber past 70% and red past 90%. |
| Memory | Peak memory for the reply |
| Ended | Shown only when the reply was cut short (see "Long or broken speech" below) |

**Performance** hides or shows the row. **Conversation** and **Log** open a panel at the side with the running transcript or the model log; the tabs at its top switch between them. Every live turn also lands in the main conversation, so you can scroll back through it afterwards. A log left open stays open when live mode ends.

- **Interrupt** stops it mid-answer and listens again.
- **Mute** stops listening until you unmute.
- **Web** is the same switch as under the message box.
- **Let me talk over it** lets you cut in by speaking while it talks. Use headphones for this: through speakers it can hear itself.
- **End** or Esc closes live mode and releases the microphone.

The sphere is a light-mode port of [Speaking Orb](https://github.com/aqualang89/shipnotes-components) by Ship Notes (MIT), in `static/orb.js`.

### Tools

The **Tools** switch under the message box lets the model call these. Each call shows as a card in the reply: open **Details** for the exact arguments and result.

| Tool | What it does |
|---|---|
| Clock | Current date and time, in any time zone |
| Calculator | Exact arithmetic, including percentages |
| Save a reminder / List reminders | Stores reminders in `data/reminders.json`. They don't notify you. |
| Web search | Searches DuckDuckGo (Bing if DuckDuckGo returns nothing) and gives the model the top 5 results |
| Open a page | Opens an address, or a search result by its number, and gives the model the page's text and its first 15 links |
| Follow a link | Opens one of those links by its number |

The first four run on your Mac and never touch the network. The web tools are offered only when **Web** is on as well (it's off by default). Their cards show a screenshot of what the browser saw; click it for the full size.

The web tools drive a separate headless Chrome with an empty profile, so it's never signed in to anything. It refuses addresses on this Mac and on your local network (`localhost`, `192.168.x.x`, `*.local` and so on), so a web page can't steer the model into reading local services.

With Web on, the model searches more than it needs to. It often looks up things it already knows, like the capital of France. Changing the tool descriptions and the system prompt didn't fix this, so turn Web on only when you want it. "Look up the latest Python release" on the empty page searches the web for that one message without turning the switch on.

## Limits worth knowing

It's a 2-billion-parameter model.

- It reads, transcribes, translates, describes and calls tools well.
- It does get facts wrong with confidence. For example, its "fun facts" about the moon aren't reliable.
- It can slip on multi-step reasoning.
- It can also write a wrong expression for the calculator. The calculator's example in the tool description is there to steer it.
- In a spoken reply it answers right after one tool step: it won't search and then open a page before speaking.
- Its context holds 32K tokens, prompt and reply together. That's Liquid's stated figure; `config.json` lists 128,000 positions, but the app plans around 32K. An image can take up to 2,048 tokens. When a conversation outgrows the context, the earliest turns are left out (the reply says how many). A single message that's too long on its own, a long video say, is refused with the reason.

## How it works

```
browser  ──HTTP──>  app/server.py (FastAPI)  ──stdin/stdout JSON lines──>  app/worker.py (MLX model)
                    serves the page, converts media                        loads the model once,
                    with ffmpeg, runs the tool loop,                        runs one job at a time,
                    starts/stops the worker                                 streams text and speech
                          │
                          └── app/browser.py: headless Chrome (Playwright) for the web tools
```

The model lives in its own process, so Stop really releases its memory and Restart is a clean reload.

The page keeps the conversation and sends all of it on every turn; the server sends it on to the model. Attachments are converted once, when you add them: audio to 16 kHz WAV, video to H.264 plus its soundtrack, HEIC to JPEG. Audio that peaks near full scale is turned down to peak at 70% (see below). They're kept until the app restarts, and the most recent 8 stay in the model's context.

Speech streams as it's made:

- The worker decodes the model's audio codes in small pieces, each with 32 frames of context before it, and sends them as 16-bit PCM.
- The page plays them back to back with Web Audio.
- The first sound arrives about 0.3 seconds after speech starts. The model makes speech two to three times faster than real time, so playback doesn't run dry.
- The whole reply is also saved as a WAV, which you can replay from the reply.

After a voice message is answered, the model transcribes it while you listen to the reply. Later turns carry the transcript instead of the recording, which keeps long conversations fast.

The tool loop works like this:

1. The tool schemas go in the chat template.
2. The model writes `<tool_call>` blocks.
3. The server runs each call (`app/tools.py`), adds the result to the conversation and asks again, up to 6 rounds.

Older tool results, such as whole web pages, are cut to 1,200 characters in later turns.

A few prompt details matter with this model. Each one was found by testing:

- **Text replies** add the model card's `Respond with text.` to the system prompt and block the speech tokens. Without that, the model sometimes answers in speech when you asked for text.
- **Spoken replies with tools** let the model choose between a tool call and speech. Its first token decides: `<tool_call>`, a speech marker, or the start of a written answer. It never wrote a sentence before a tool call in 24 tries, typed and spoken. A written answer is stopped at that first token and the model is asked again for speech, instead of writing the whole answer first (a short story wasted 7 seconds that way). After a tool result it is always asked to speak.
- **Spoken answers after a web search** get a note beside the results asking for two or three sentences and at most three results. Without it the model read out every title, and a list of five books ran to 35-50 seconds of speech. With it, answers came to 44-57 words in 12 of 12 tries. In the system prompt the same request worked half the time.
- **An attached recording** is introduced with "Here is a recording:". Without that, "Transcribe this" on a recording that says "remind me to…" saves a reminder instead of transcribing.
- **A video's soundtrack** is followed by a short note saying it belongs to the video. Without that, the model ignores the sound whenever tools are offered.
- **Transcripts of voice messages** use a system prompt that makes the model a transcription service. Without it, it answers a spoken question instead of writing it down: "What is 17 plus 25?" came back as "42".
- **Very loud recordings are turned down.** The browser's automatic gain can push a live-mode turn up to full scale. At that level "What time is it in Tokyo right now?" was transcribed as "I don't know the current time in Tokyo." The same recording at 70% volume or lower came back word for word.
- **There's no "use the tools" instruction** in the system prompt. The schemas are enough, and with such an instruction the model called the clock before unrelated spoken replies.

### Long or broken speech

In a spoken reply, text and speech are made in turns, and the text runs ahead: it can finish 20 seconds before the speech. The speech then carries on alone. Now and then it loses its place there and makes rumble or a held vowel until it hits the token limit. One reply that read out search results was clear for 24 seconds and then noise for 45. The worker has three guards:

- **Noise.** Speech has gaps between words, so within any two seconds its level swings widely: at least 19 dB in 184 good replies. The noise swung 3-10 dB. The worker measures this as it decodes; two two-second windows in a row under 12 dB stop the reply, and the saved speech is cut where the noise began. The reply's note says "speech cut where it turned to noise".
- **Overrun.** Speech that runs on past what its text takes to say (0.8 seconds per word, digit or CJK character, plus 5 seconds) is stopped.
- **Context.** The worker counts the prompt before the model reads it and caps the reply at what's left of the 32K context.

Speech is sampled as the model card recommends (temperature 0.4, repetition penalty 1.2). Generated speech and page screenshots go to `data/outputs/` and are cleared after a day.

## Tests

With the app running (model stopped or not):

```bash
.venv/bin/python tests/smoke_test.py
```

It drives the same API the page uses, with real media, and ends with the model stopped. It covers:

- every attachment type, including webm and m4a recordings, a HEIC photo and a silent video;
- multi-turn context, including media from earlier turns;
- each tool, plus checks that transcribing or summarizing a recording calls no tool;
- spoken replies with and without tools, and that the streamed speech matches the saved file;
- a reply it starts to write being asked to speak at once, and a spoken answer after a web search staying short;
- a conversation past the 32K context leaving out its earliest turns, and a single message too long for it being refused;
- the noise guard on a real reply that turned to noise (`tests/fixtures/speech_turns_to_noise.opus`) and on clean speech, and which checkpoint `start.sh` picks for different Macs;
- transcripts of voice messages, a later turn that relies on one, and a very loud live-mode recording (`tests/fixtures/loud_live_turn.wav`, a real voice kept out of the repository: put any recording of "What time is it in Tokyo?" there that peaks at full scale, or the check is skipped);
- web search, opening a page, following a link, refusing local addresses, and Web being off unless asked for;
- read-aloud in English and Arabic;
- stopping a written or spoken answer, bad input and restarting;
- stopping the model mid-answer and recovering from a crashed model process.

The web checks need an internet connection.

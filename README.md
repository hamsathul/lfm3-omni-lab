# LFM3-Omni Lab

One conversation page for [LFM3-Omni-2B](https://huggingface.co/LiquidAI/LFM3-Omni-2B) on an Apple Silicon Mac or a Windows PC.

- Type to it, talk to it, or attach images, recordings and videos.
- It answers in writing or out loud.
- It can call a few tools on your computer, and it can search and read the web when you allow it.
- Live mode lets you hold a spoken conversation without touching anything.
- A native window (`native/`, for macOS and Windows) holds the same conversation outside the browser.

The model runs on your Mac with MLX, or on a PC with [llama.cpp](https://github.com/ggml-org/llama.cpp) ([Windows](#on-windows)). Nothing leaves the machine after the one-time model download, unless you turn on Web.

## Start

```bash
cd ~/projects/lfm3-omni-lab
./start.sh
```

It needs a Mac with Apple Silicon (M1 or later) and Python 3.10 or newer. macOS's own `python3` is 3.9, so the script looks for a newer one (`brew install python` if there isn't one).

The first run creates `.venv` and installs `mlx-vlm`, which takes a minute or two. Then the script picks the model checkpoint for this Mac and downloads it if it isn't already here:

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

Audio, recording and video need `ffmpeg` (`brew install ffmpeg`). Web needs [agent-browser](https://github.com/vercel-labs/agent-browser): `brew install agent-browser`, then `agent-browser install` to download the Chrome it drives.

| Setting | How |
|---|---|
| Another port | `PORT=8766 ./start.sh` |
| Another checkpoint (e.g. bf16, or a local folder) | `LFM3_MODEL=LiquidAI/LFM3-Omni-2B-MLX-bf16 ./start.sh` |
| Load the model as soon as the app starts | `LFM3_AUTOSTART=1 ./start.sh` |
| Don't open a browser tab | `NO_BROWSER=1 ./start.sh` |
| Watch the web browser work in a visible window | `LFM3_BROWSER_HEADED=1 ./start.sh` |

### Setting it up by hand

`start.sh` does all of this for you. To do it yourself:

```bash
git clone https://github.com/hamsathul/lfm3-omni-lab.git
cd lfm3-omni-lab
python3.12 -m venv .venv                      # any arm64 Python 3.10 or newer
.venv/bin/pip install -r requirements.txt
brew install ffmpeg agent-browser             # ffmpeg for audio and video, agent-browser for Web
agent-browser install                         # downloads the Chrome that agent-browser drives
export LFM3_MODEL="$(.venv/bin/python -m app.pick_model)"   # picks and downloads the checkpoint
.venv/bin/python -m uvicorn app.server:app --host 127.0.0.1 --port 8765
```

Then open http://localhost:8765.

### On Windows

Double-click `start.bat`, or in PowerShell:

```powershell
cd lfm3-omni-lab
.\start.bat
```

It needs 64-bit Windows 10 or 11. The first run fetches what's missing, which takes a few minutes:

- Python 3.12, with `winget`, if there's no 64-bit Python 3.10 or newer, then `.venv` and the packages;
- llama.cpp's `llama-server` (release b12223) into `llama.cpp\`: the CUDA 13 build for an NVIDIA card with driver 580 or newer, CUDA 12 for an older driver, Vulkan for other cards;
- `ffmpeg`, with `winget`;
- the model, [`LiquidAI/LFM3-Omni-2B-GGUF`](https://huggingface.co/LiquidAI/LFM3-Omni-2B-GGUF): Q8_0 (2.4 GB with its audio and vision part), or Q4_K_M (1.4 GB) on an NVIDIA card with less than 4 GB of memory.

Web needs agent-browser as on a Mac (`npm install -g agent-browser`, then `agent-browser install`); `start.ps1` reminds you. Everything else works the same as on a Mac, with these differences:

- **Spoken replies are written first, then read out.** llama.cpp doesn't make the model's own interleaved speech, so the text streams as usual and each sentence is spoken with the model's text-to-speech voice as soon as it's written. The first sound comes about a sentence later than on a Mac.
- Spoken answers after a web search run a little longer (up to about 85 words, against 44-57 with MLX).
- **Memory** in the performance row is the graphics card's memory the model uses, from `nvidia-smi` (with other cards, llama-server's own memory).
- HEIC photos are converted with ffmpeg.

| Setting (PowerShell) | How |
|---|---|
| Another port | `$env:PORT=8766; .\start.bat` |
| Another quantization, or your own GGUF file | `$env:LFM3_MODEL='LiquidAI/LFM3-Omni-2B-GGUF:Q4_K_M'` (or a file or folder path) |
| Another llama.cpp build | `$env:LFM3_LLAMA_BUILD='vulkan'` (`cuda-13.5`, `cuda-12.4`, `vulkan` or `cpu`; delete `llama.cpp\` to download again) |
| Your own `llama-server.exe` | `$env:LFM3_LLAMA_SERVER='C:\path\to\llama-server.exe'` |
| Extra `llama-server` options | `$env:LFM3_LLAMA_ARGS='-ngl 20'` |
| The male voice for spoken replies | `$env:LFM3_VOICE='male'` |

`LFM3_AUTOSTART`, `NO_BROWSER` and `LFM3_BROWSER_HEADED` work as on a Mac. The same llama.cpp path runs on a Mac or Linux with `LFM3_BACKEND=llamacpp` and `LFM3_LLAMA_SERVER` pointing at a `llama-server`, which is how it's tested on a Mac.

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

To show it something, click **Attach**, drop a file on the window or paste one. A photo, video or recording waits above the buttons and goes out with the next thing you say, in the same message, so "what's this?" means the new one. While you talk about it, the photo stays beside the sphere. Anything already attached in the chat comes along into live mode, and anything you attached but didn't ask about goes back to the message box when live mode ends.

While it works, a sphere of particles shows what it's doing:

- grey and breathing slowly while it waits for you;
- leaning toward you while you talk, with your voice running up it in ripples, finer for a higher voice;
- pulled into a small, fast-turning core for a moment when your turn ends ("Got it");
- a blue knot while it thinks;
- amber rings while it searches the web, and a page of lines with a scan line moving down it while it reads one;
- a green sphere while it speaks, pulsing with the speech;
- a flat grey disc while it's muted;
- a red shudder that loosens and sags when something goes wrong.

A change of state spreads across the sphere as a wave rather than all at once.

**Orb** in the top bar swaps the sphere for one of 19 orbs from [VoiceOrbs](https://voiceorbs.vercel.app/) by Alexis Munoz (MIT), such as Siri sheet, Plasma or Nebula. They have fewer states than the sphere, so some share one look:

- waiting and you talking both follow your voice;
- "Got it", thinking, searching and reading share their thinking look;
- speaking follows the reply's audio;
- muted and errors have their own.

The captions work the same with any of them, and the choice is remembered. These orbs are React components, so their file carries React (370 KB) and loads only when you pick one; Nebula's three.js scene (880 KB more) loads only when you pick Nebula.

To compare them, open http://localhost:8765/orb-preview.html. **Play a turn** runs a sample conversation through every state with a recorded voice, **All orbs** shows all twenty at once, and **Use in live mode** picks the one shown.

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

The sphere is a light-mode port of [Speaking Orb](https://github.com/aqualang89/shipnotes-components) by Ship Notes (MIT), in `static/orb.js`. The orbs from VoiceOrbs and [Thinking Orbs](https://libraries.dev/orbs) are built into `static/vendor/orbs`, with their licences; the README there says what was changed, and `tools/orbs/build.sh` rebuilds them. `static/orb-skins.js` connects them to the page.

### Tools

The **Tools** switch under the message box lets the model call these. Each call shows as a card in the reply: open **Details** for the exact arguments and result.

While a tool runs, its card shows a small dotted orb from [Thinking Orbs](https://libraries.dev/orbs) by Jakub Antalik (MIT), one for each kind of work: a turning globe for a web search, a constellation wiring itself for opening a page or acting on it, scrambling bands for the calculator, and orbiting dots for the clock and reminders. Blue means a tool on your Mac, amber the web. A tick or a cross takes its place when the tool finishes. Live mode shows the same orb in the line under your words.

| Tool | What it does |
|---|---|
| Clock | Current date and time, in any time zone |
| Calculator | Exact arithmetic, including percentages |
| Save a reminder / List reminders | Stores reminders in `data/reminders.json`. They don't notify you. |
| Web search | Searches DuckDuckGo (Bing if DuckDuckGo returns nothing) and gives the model the top 5 results |
| Open a page | Opens an address, or a search result by its number, and gives the model the page's text and a numbered list of up to 30 things on it it can use: links in the article, buttons, text boxes |
| Click | Clicks one of those by its number: a link, a button, a checkbox, a cookie banner's Accept |
| Type | Types into a text box by its number, and presses Enter if the model asks to (a site's own search, a form) |
| Go back | Returns to the previous page |

The first four run on your Mac and never touch the network. The web tools are offered only when **Web** is on as well (it's off by default). Their cards show a screenshot of what the browser saw; click it for the full size.

The web tools drive a headless Chrome through [agent-browser](https://github.com/vercel-labs/agent-browser) (`brew install agent-browser` or `npm install -g agent-browser`, then `agent-browser install` once; `start.sh` reminds you). The model can do several steps in one reply: "Open Wikipedia, search it for the Eiffel Tower and tell me how tall it is" opens the page, types into its search box and answers.

It's kept private:

- It has its own agent-browser session and settings file, and a fresh, empty Chrome profile each time it starts, so it's never signed in to anything and never sees your Chrome or your agent-browser setup.
- Every request it makes goes through a small proxy inside the app (`app/netguard.py`), which looks each address up itself and connects only to the public internet. Addresses on this Mac or your network (`localhost`, `192.168.x.x`, `10.x`, Tailscale's `100.x`, `*.local`) are refused, and so are public names that point at them (`localtest.me`), whether the model opens them, clicks its way there, or a page loads them in the background.
- What the model types goes to agent-browser as data, never through a shell.

With Web on, the model searches more than it needs to. It often looks up things it already knows, like the capital of France. Changing the tool descriptions and the system prompt didn't fix this, so turn Web on only when you want it. "Look up the latest Python release" on the empty page searches the web for that one message without turning the switch on.

## Native window

`native/` holds the same conversation as a macOS or Windows app, so it can live in the Dock or the taskbar instead of a browser tab. It's written in Rust with [Rune](https://github.com/anomalyco/rune) and talks to the lab server the same way the page does, so the server still has to run: start it with `./start.sh`, or with the window's **Start lab server** button.

### Downloading it

Each [release](https://github.com/hamsathul/lfm3-omni-lab/releases) has the app ready-built for Apple Silicon: `LFM3-Omni-<version>-macOS-arm64.zip`. Unzip it and move `LFM3-Omni.app` to Applications. Releases from 0.2.0 also have `LFM3-Omni-<version>-windows-x64.zip`, which holds `LFM3-Omni.exe`; it isn't signed, so the first time Windows SmartScreen stops it: click **More info**, then **Run anyway**.

It isn't notarized by Apple, so the first time you open it macOS refuses. Open **System Settings → Privacy & Security** and click **Open Anyway** next to the note about LFM3-Omni. Or remove the download's quarantine flag yourself: `xattr -dr com.apple.quarantine /Applications/LFM3-Omni.app`.

When it can't find the lab, it offers two buttons:

- **Set up the lab** downloads the lab for its version from GitHub into `~/lfm3-omni-lab` (on Windows, `%USERPROFILE%\lfm3-omni-lab`) and runs `start.sh` (`start.ps1` on Windows). On its first run that makes the Python environment, installs the packages and downloads the model (about 3 GB) if it isn't there yet. The window shows how far it has got.
- **Choose lab folder…** points it at a copy you already have.

On a Mac the lab itself still needs Python 3.10 or newer (`brew install python`). Audio and video need `ffmpeg`, and Web needs `agent-browser` (see [Start](#start)). On Windows `start.ps1` installs Python and ffmpeg itself (see [On Windows](#on-windows)).

### Building it

You need:

- macOS 13 or later;
- the Xcode Command Line Tools, for the linker and `codesign`: `xcode-select --install`;
- a current stable Rust from [rustup](https://rustup.rs): `curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh`. It's tested with 1.99; if an older one won't build it, run `rustup update`.

Then build and install the app:

```bash
cd lfm3-omni-lab/native
./bundle.sh --install
```

`bundle.sh` makes a release build with `cargo build --release`. The first build downloads Rune from GitHub at a pinned commit and compiles everything, which takes a few minutes; later builds take seconds. It then wraps the program in `native/dist/LFM3-Omni.app` with its icon and the text macOS shows when it asks for the microphone, and signs it ad hoc so macOS remembers that permission. `--install` copies the app to `~/Applications`. Open it from there or from Spotlight, and drag it to the Dock to keep it.

| To | Run, in `native/` |
|---|---|
| Build the app without installing it | `./bundle.sh` (the app is `native/dist/LFM3-Omni.app`) |
| Build just the program | `cargo build --release` (it's `native/target/release/lfm3-omni`) |
| Run it without making an app | `cargo run --release` |
| Update it after a `git pull` | `./bundle.sh --install` |
| Make the zip for a release | `./bundle.sh --dist` (it's `native/dist/LFM3-Omni-<version>-macOS-arm64.zip`) |

**Start lab server** runs `start.sh` in the lab folder. To find that folder, the app looks in this order:

1. `LFM3_LAB_DIR`;
2. a folder chosen in the app;
3. the checkout it was built from (not for a `--dist` build, which is made for other computers);
4. `lfm3-omni-lab` in your home folder, or in `projects`, `Developer`, `code`, `src`, `GitHub`, `Documents`, `Desktop` or `Downloads`.

`--dist` also keeps this computer's folder names out of the program. macOS asks for the microphone the first time you record or open live mode, and may ask again after a rebuild, since each build has a new signature.

#### On Windows

You need Rust from [rustup](https://rustup.rs) with its default MSVC toolchain, and Visual Studio's C++ build tools ("Desktop development with C++" in the [Build Tools for Visual Studio](https://visualstudio.microsoft.com/visual-cpp-build-tools/), or `winget install Microsoft.VisualStudio.2022.BuildTools`), which rustup offers to install. Then, in PowerShell:

```powershell
cd lfm3-omni-lab\native
powershell -ExecutionPolicy Bypass -File bundle.ps1 -Install
```

That builds `native\dist\LFM3-Omni.exe`, with its icon, copies it to `%LOCALAPPDATA%\Programs\LFM3-Omni` and adds **LFM3-Omni** to the Start menu. `-Dist` makes the release zip (`native\dist\LFM3-Omni-<version>-windows-x64.zip`) instead. **Start lab server** runs `start.ps1`, hidden, with its output in `data\native-server.log`.

### What it does

It has:

- typed chat with streamed, formatted replies, the **Tools** and **Web** switches, and **Auto**, **Text** and **Voice** replies;
- attachments from the + button or dragged onto the window, with picture thumbnails;
- voice messages from the microphone button, with what it heard shown under the message;
- spoken replies that start playing while the rest is made, and **Play** under each one;
- **Stop** or Esc to end a reply, and **Retry** when one fails;
- live mode (the orb at the end of the message bar, or Cmd+L; Ctrl+L on Windows) with the page's voice detector, **Mute**, **Interrupt** and a **Talk over** switch;
- the model's status with Start and Stop, and **Start lab server** when the server isn't running. That runs `start.sh` with its output in `data/native-server.log`, and quitting the app stops it again;
- Cmd+N (Ctrl+N on Windows) for a new chat.

The reply mode, the three switches and a lab folder chosen in the app are remembered in `~/Library/Application Support/LFM3-Omni Native/prefs.json` (`%APPDATA%\LFM3-Omni Native\prefs.json` on Windows).

It doesn't have the page's Settings (system prompt, temperature, reply length, read-aloud voice), the reminders list, the Log panel or Read aloud for written replies. Use the page for those; both can be open at once.

The window has no echo cancellation, which the page gets from the browser. With **Talk over** on and the replies coming from the Mac's speakers, the model can hear itself and cut itself off, so use headphones for that. With Talk over off (the default) it doesn't listen while it speaks.

| Setting | How |
|---|---|
| Another server | `--server http://127.0.0.1:8766`, or `LFM3_SERVER` |
| Another lab folder (for Start lab server) | `LFM3_LAB_DIR=/path/to/lab` |
| Open in live mode | `--live` |
| Print event timings and the audio formats in use | `LFM3_NATIVE_LOG=1` |

## Limits worth knowing

It's a 2-billion-parameter model.

- It reads, transcribes, translates, describes and calls tools well.
- It does get facts wrong with confidence. For example, its "fun facts" about the moon aren't reliable.
- It can slip on multi-step reasoning.
- It can also write a wrong expression for the calculator. The calculator's example in the tool description is there to steer it.
- In a spoken reply it answers right after one tool step: it won't search and then open a page before speaking.
- With **Web** on it searches for things it doesn't need to, and it sometimes calls the clock when it should search or save a reminder.
- In a later turn, a spoken percentage can lose its % sign on the way to the calculator: "What's seventeen and a half percent of 2,340?" came back as 40,950 (2 of 2). Typed, it works.
- It sees photos and videos only from the latest message that brought any (see "How it works"). To compare two, send them together.
- Its context holds 32K tokens, prompt and reply together. That's Liquid's stated figure; `config.json` lists 128,000 positions, but the app plans around 32K. An image can take up to 2,048 tokens. When a conversation outgrows the context, the earliest turns are left out (the reply says how many). A single message that's too long on its own, a long video say, is refused with the reason.

## How it works

```
browser  ──HTTP──>  app/server.py (FastAPI)  ──stdin/stdout JSON lines──>  app/worker.py (MLX model, on a Mac)
                    serves the page, converts media                        loads the model once,
                    with ffmpeg, runs the tool loop,                        runs one job at a time,
                    starts/stops the worker                                 streams text and speech
                          │                                          or    app/worker_llamacpp.py (elsewhere)
                          │                                                 the same protocol, in front of
                          │                                                 llama-server on a local port
                          └── app/browser.py: a private headless Chrome (agent-browser) for the web tools,
                              every request through app/netguard.py's local-address guard
```

The model lives in its own process, so Stop really releases its memory and Restart is a clean reload. On llama.cpp the worker starts `llama-server` with two slots: one keeps the conversation's prompt cache, and the other makes speech with `/tts` while the next sentence is still being written. `llama-server` is tied to the worker (a job object on Windows, the parent-death signal on Linux, the server ending the worker's process group on a Mac), so it goes when the worker does, even after a crash. Its log is `data/llama-server.log`; the lines that matter also show in **Log**.

The page keeps the conversation and sends all of it on every turn; the server sends it on to the model. Attachments are converted once, when you add them: audio to 16 kHz WAV, video to H.264 plus its soundtrack, HEIC to JPEG. Audio that peaks near full scale is turned down to peak at 50% (see below). They're kept until the app restarts, and the most recent 8 stay in the model's context.

Speech streams as it's made:

- The worker decodes the model's audio codes in small pieces, each with 32 frames of context before it, and sends them as 16-bit PCM.
- The page plays them back to back with Web Audio.
- The first sound arrives about 0.3 seconds after speech starts. The model makes speech two to three times faster than real time, so playback doesn't run dry.
- The whole reply is also saved as a WAV, which you can replay from the reply.

Before a voice message is answered, the model writes down what it says, so your words show up straight away. From the second turn on, the transcript goes in beside the recording. Later turns carry the transcript instead of the recording, which keeps long conversations fast.

Photos and videos are shown to the model only from the latest message that brought any. Earlier ones are replaced by a short note, and what the model said about them stays in the conversation. To compare two photos, send them in the same message.

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
- **After the first answer, a voice message goes in with its transcript beside it.** Without it, the model sometimes ignored what was said and copied an earlier answer. Asked "Tell me about yourself", then something it couldn't make out, it described itself both times, and then gave the same description to a clear "Tell me a story" (3 of 3 tries with Web on). With the transcript beside the audio it told a story (3 of 3). On a first turn the transcript isn't added: there it made spoken questions call the clock ("Tell me one fun fact about the moon", 4 of 4), and there's no earlier answer to copy. A note in the system prompt asking it to answer the latest message didn't help.
- **Only the newest photos and videos are shown.** With an earlier photo still in view, "What's in this picture?" about a new one described the old one in 4 of 4 tries, and a note saying which photo was new didn't help. Showing only the newest got all 14 test turns right, follow-ups included, and the model still answered "What did the first sign say?" from its earlier reply.
- **Transcripts of voice messages** use a system prompt that makes the model a transcription service. Without it, it answers a spoken question instead of writing it down: "What is 17 plus 25?" came back as "42".
- **Very loud recordings are turned down.** The browser's automatic gain can push a live-mode turn up to full scale. At that level "What time is it in Tokyo right now?" was transcribed as "I don't know the current time in Tokyo." The same recording at 50% volume came back word for word (MLX managed at 70%, llama.cpp only at 50% or lower).
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
- multi-turn context, including media from earlier turns, and a new photo being the one described rather than an earlier one;
- a spoken question that follows two alike answers getting its own answer, not a copy;
- each tool, plus checks that transcribing or summarizing a recording calls no tool;
- spoken replies with and without tools, and that the streamed speech matches the saved file;
- a reply it starts to write being asked to speak at once, and a spoken answer after a web search staying short;
- a conversation past the 32K context leaving out its earliest turns, and a single message too long for it being refused;
- the noise guard on a real reply that turned to noise (`tests/fixtures/speech_turns_to_noise.opus`) and on clean speech, and which checkpoint `start.sh` picks for different Macs;
- transcripts of voice messages, a later turn that relies on one, and a very loud live-mode recording (`tests/fixtures/loud_live_turn.wav`, a real voice kept out of the repository: put any recording of "What time is it in Tokyo?" there that peaks at full scale, or the check is skipped);
- web search, opening a page, clicking a link, typing into a site's search box, going back, refusing local addresses, and Web being off unless asked for;
- read-aloud in English and Arabic;
- stopping a written or spoken answer, including while a voice message is still being written down, bad input and restarting;
- stopping the model mid-answer and recovering from a crashed model process.

The web checks need an internet connection.

On Windows run `.venv\Scripts\python tests\smoke_test.py`. Its spoken test questions are made with the Windows speech voices instead of the Mac's `say`, the HEIC check is skipped, and the checkpoint checks cover the GGUF choice instead.

The native window has its own checks, which need no server:

```bash
cd native
cargo test                    # the voice detector, audio, Markdown, API and lab-finding pieces
cargo test -- --ignored       # plays near-silence on the real speaker, and downloads the lab from GitHub
cargo run -- --snap chat.png --scene chat    # draws one screen; see src/scenes.rs for the rest
```

With a server running, `LFM3_NATIVE_NET=1 LFM3_NATIVE_REALTIME=1 cargo run -- --script tests/scripts/chat-tools.json --out frames -- --server http://127.0.0.1:8765` drives the window through a real conversation without opening it. The scripts in `native/tests/scripts/` cover tools, a spoken reply, an image, a voice message (add `--hear ../static/samples/speech.wav` at the end) and stopping a reply.

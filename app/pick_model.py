"""Choose the LFM3-Omni checkpoint for this Mac, and download it if it isn't here yet.

start.sh runs this before the app starts. It prints the chosen model id on stdout; anything
else (the reasoning, download progress) goes to stderr.

  .venv/bin/python -m app.pick_model

LFM3_MODEL picks a checkpoint by hand (a Hugging Face id or a local folder). Without it:
- a Mac with less than 16 GB of memory gets the 4-bit checkpoint (2.1 GB);
- anything bigger gets the 8-bit one (2.9 GB), which this app is tested with;
- a checkpoint that's already downloaded and fits the Mac is used as it is, so nobody
  downloads a second copy. bf16 (4.5 GB) is only used when it's already here or asked for:
  on a base chip it can fall behind real time while speaking, and the speech then stutters.
"""

import os
import subprocess
import sys
from pathlib import Path

REPO = "LiquidAI/LFM3-Omni-2B-MLX-{}"
SIZES_GB = {"4bit": 2.1, "8bit": 2.9, "bf16": 4.5}
NEEDS_MEMORY_GB = {"4bit": 0, "8bit": 16, "bf16": 16}
REQUIRED = ["config.json", "tokenizer.json", "audio_tokenizer/config.json"]


def say(text):
    print(text, file=sys.stderr, flush=True)


def memory_gb():
    out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout
    return int(out.strip()) / 2**30


def complete(folder):
    """True when a downloaded snapshot has every file the model needs."""
    folder = Path(folder)
    if not all((folder / name).is_file() for name in REQUIRED):
        return False
    index = folder / "model.safetensors.index.json"
    if index.is_file():
        import json
        shards = set(json.loads(index.read_text())["weight_map"].values())
        return all((folder / shard).is_file() for shard in shards)
    return (folder / "model.safetensors").is_file()


def local_copy(repo_id):
    from huggingface_hub import snapshot_download
    try:
        folder = snapshot_download(repo_id, local_files_only=True)
    except Exception:
        return None
    return folder if complete(folder) else None


def download(repo_id, size_gb=None):
    from huggingface_hub import snapshot_download
    say(f"Downloading {repo_id}" + (f" ({size_gb} GB) from Hugging Face..." if size_gb else " from Hugging Face..."))
    try:
        folder = snapshot_download(repo_id)
    except Exception as exc:
        say(f"Couldn't download {repo_id}: {exc}")
        say("Check the internet connection and run ./start.sh again; a partial download resumes.")
        sys.exit(1)
    if not complete(folder):
        say(f"The download of {repo_id} is missing files. Run ./start.sh again to resume it.")
        sys.exit(1)
    return folder


def main():
    chosen = os.environ.get("LFM3_MODEL", "").strip()
    if chosen:
        if Path(chosen).is_dir():
            say(f"Model: the local folder {chosen}")
        elif local_copy(chosen):
            say(f"Model: {chosen} (already downloaded)")
        else:
            download(chosen)
        print(chosen)
        return

    memory = memory_gb()
    best = "8bit" if memory >= NEEDS_MEMORY_GB["8bit"] - 0.5 else "4bit"
    fits = [q for q in ("8bit", "bf16", "4bit") if memory >= NEEDS_MEMORY_GB[q] - 0.5]
    for quant in [best] + [q for q in fits if q != best]:
        if local_copy(REPO.format(quant)):
            note = "" if quant == best else (f" (already downloaded; the usual pick for {memory:.0f} GB is {best}:"
                                             f" LFM3_MODEL={REPO.format(best)} ./start.sh)")
            say(f"Model: {REPO.format(quant)}{note}")
            print(REPO.format(quant))
            return
    say(f"This Mac has {memory:.0f} GB of memory, so the lab uses the {best} checkpoint.")
    download(REPO.format(best), SIZES_GB[best])
    print(REPO.format(best))


if __name__ == "__main__":
    main()

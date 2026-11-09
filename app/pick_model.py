"""Choose the LFM3-Omni checkpoint for this computer, and download it if it isn't here yet.

start.sh and start.ps1 run this before the app starts. It prints the chosen model id on stdout;
anything else (the reasoning, download progress) goes to stderr.

  .venv/bin/python -m app.pick_model

LFM3_MODEL picks a checkpoint by hand (a Hugging Face id or a local folder). Without it, on a
Mac (MLX):
- a Mac with less than 16 GB of memory gets the 4-bit checkpoint (2.1 GB);
- anything bigger gets the 8-bit one (2.9 GB), which this app is tested with;
- a checkpoint that's already downloaded and fits the Mac is used as it is, so nobody
  downloads a second copy. bf16 (4.5 GB) is only used when it's already here or asked for:
  on a base chip it can fall behind real time while speaking, and the speech then stutters.

Elsewhere (llama.cpp, or LFM3_BACKEND=llamacpp) it's the GGUF build, model and mmproj:
- Q8_0 (2.4 GB), which this app is tested with;
- Q4_K_M (1.4 GB) on an NVIDIA card with less than 4 GB of memory.
LFM3_MODEL takes LiquidAI/LFM3-Omni-2B-GGUF:<quantization>, a folder or a .gguf file there.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = "LiquidAI/LFM3-Omni-2B-MLX-{}"
SIZES_GB = {"4bit": 2.1, "8bit": 2.9, "bf16": 4.5}
NEEDS_MEMORY_GB = {"4bit": 0, "8bit": 16, "bf16": 16}
REQUIRED = ["config.json", "tokenizer.json", "audio_tokenizer/config.json"]
GGUF_REPO = "LiquidAI/LFM3-Omni-2B-GGUF"
GGUF_SIZES_GB = {"Q8_0": 2.4, "Q4_K_M": 1.4, "Q4_0": 1.3, "F16": 4.4, "BF16": 4.4}


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


def gpu_memory_mb():
    smi = shutil.which("nvidia-smi")
    if not smi:
        return None
    try:
        out = subprocess.run([smi, "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10).stdout
        return float(out.split()[0])
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None


def gguf_files(repo, quant):
    name = repo.split("/")[-1].removesuffix("-GGUF")
    return [f"{name}-{quant}.gguf", f"mmproj-{name}-{quant}.gguf"]


def gguf_main():
    chosen = os.environ.get("LFM3_MODEL", "").strip()
    if chosen and Path(chosen).exists():
        say(f"Model: {chosen}")
        print(chosen)
        return
    if chosen:
        repo, _, quant = chosen.partition(":")
    else:
        vram = gpu_memory_mb()
        repo, quant = GGUF_REPO, ("Q4_K_M" if vram is not None and vram < 4096 else "Q8_0")
        if quant != "Q8_0":
            say(f"This GPU has {vram / 1024:.1f} GB of memory, so the lab uses the smaller Q4_K_M model.")
    quant = (quant or "Q8_0").upper()
    from huggingface_hub import hf_hub_download
    files = gguf_files(repo, quant)
    missing = []
    for filename in files:
        try:
            hf_hub_download(repo, filename, local_files_only=True)
        except Exception:
            missing.append(filename)
    if not missing:
        say(f"Model: {repo} {quant} (already downloaded)")
    else:
        size = GGUF_SIZES_GB.get(quant)
        say(f"Downloading {repo} {quant}" + (f" ({size} GB)" if size else "") + " from Hugging Face...")
        for filename in missing:
            try:
                hf_hub_download(repo, filename)
            except Exception as exc:
                say(f"Couldn't download {filename}: {exc}")
                say("Check the internet connection and start the lab again; a partial download resumes.")
                sys.exit(1)
    print(f"{repo}:{quant}")


def backend():
    return os.environ.get("LFM3_BACKEND") or ("mlx" if sys.platform == "darwin" else "llamacpp")


def main():
    if backend() != "mlx":
        gguf_main()
        return
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

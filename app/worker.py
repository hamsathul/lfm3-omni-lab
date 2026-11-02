"""Model worker: loads LFM3-Omni once, then answers jobs read from stdin.

It runs as its own process so that stopping it really frees the model's memory.

Protocol, one JSON object per line:
  stdin   {"id", "messages", "tools", "image", "audio", "video", "output", "max_tokens", "temperature", "stream"}
          {"cmd": "cancel", "id"}
  stdout  {"event": "loading" | "ready" | "fatal" | "token" | "audio_chunk" | "done" | "error", ...}
Spoken replies stream: text as "token" events and 24 kHz speech as base64 16-bit PCM "audio_chunk" events.
Anything a library prints is redirected to stderr, which the app shows as the model log.
"""

import argparse
import base64
import json
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

# Keep the real stdout for the protocol; send every stray print to stderr.
_proto = os.fdopen(os.dup(1), "w", buffering=1)
os.dup2(2, 1)
sys.stdout = sys.stderr

_send_lock = threading.Lock()


def send(**message):
    with _send_lock:
        _proto.write(json.dumps(message) + "\n")
        _proto.flush()


def log(text):
    print(f"[worker] {text}", file=sys.stderr, flush=True)


def resolve_model(model_id):
    """Use the cached copy without touching the network; download only if missing."""
    from huggingface_hub import snapshot_download

    if Path(model_id).is_dir():
        return model_id
    try:
        return snapshot_download(model_id, local_files_only=True)
    except Exception as exc:
        log(f"No complete local copy of {model_id} ({type(exc).__name__}: {exc}). "
            "Downloading it from Hugging Face (about 3 GB the first time)...")
        return snapshot_download(model_id)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", required=True)
    parser.add_argument("--outputs", required=True)
    args = parser.parse_args()
    outputs = Path(args.outputs)
    outputs.mkdir(parents=True, exist_ok=True)

    send(event="loading", model=args.model)
    started = time.time()
    try:
        import mlx.core as mx
        import numpy as np
        from mlx_vlm import load, stream_generate
        from mlx_vlm.generate.common import generation_stream, wired_limit
        from mlx_vlm.utils import prepare_inputs, should_add_special_tokens

        model, processor = load(resolve_model(args.model))
        codec = model.codec()
    except Exception as exc:
        log(traceback.format_exc())
        send(event="fatal", message=f"Could not load the model: {exc}")
        return 1

    def memory_gb():
        return round((mx.get_active_memory() + mx.get_cache_memory()) / 1e9, 2)

    tokenizer = processor.tokenizer
    config = model.config
    speech_start_ids = tokenizer.convert_tokens_to_ids(["<|audio_start|>", "<|mixed_start|>"])
    stop_ids = set(np.atleast_1d(config.eos_token_id).tolist()) | {config.text_end_token_id}
    add_special = should_add_special_tokens(config.model_type, processor)
    rate = codec.config.sampling_rate
    samples_per_frame = codec.config.stft_hop_length * codec.config.stft_frame_stack  # 80 ms

    def pcm16(audio):
        return (np.clip(np.asarray(audio, dtype=np.float32), -1, 1) * 32767).astype("<i2")

    def save_wav(path, audio):
        import wave
        with wave.open(str(path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(rate)
            out.writeframes(pcm16(audio).tobytes())

    # Speech is decoded in pieces as it's generated. Each piece is decoded with 32 frames of
    # the speech before it (the codec's attention window) and holds back the newest frame,
    # whose samples still depend on the next one. Measured against decoding everything at
    # the end, the pieces differ by less than -34 dB.
    CONTEXT_FRAMES, FIRST_PIECE, PIECE = 32, 3, 6

    def decode_piece(frames, start, end):
        low = max(0, start - CONTEXT_FRAMES)
        audio = codec.decode(mx.stack(frames[low:end + 1 if end < len(frames) else end], axis=-1))
        return np.asarray(audio[(start - low) * samples_per_frame:(end - low) * samples_per_frame])
    load_seconds = round(time.time() - started, 1)
    log(f"Model loaded in {load_seconds} s")
    send(event="ready", load_seconds=load_seconds, memory_gb=memory_gb())

    jobs = queue.Queue()
    cancelled = set()

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
                cancelled.add(message.get("id"))
            else:
                jobs.put(message)
        jobs.put(None)  # stdin closed: the app is gone, so exit

    threading.Thread(target=read_stdin, daemon=True).start()

    def run(job):
        job_id = job["id"]
        # Tool schemas go into the system prompt via the chat template (<tools>...</tools>).
        prompt = processor.apply_chat_template(
            job["messages"], tools=job.get("tools") or None, add_generation_prompt=True, tokenize=False
        )
        media = {key: job.get(key) or None for key in ("image", "audio", "video")}
        options = {"max_tokens": int(job.get("max_tokens") or 1024)}
        mx.reset_peak_memory()
        started = time.time()

        if job.get("output", "text") == "text":
            options["temperature"] = float(job.get("temperature") or 0.0)
            # Backstop for text replies: never let the model switch into speech mid-answer.
            options["logit_bias"] = {token: -1e9 for token in speech_start_ids}
            pieces, last, finish = [], None, None
            for chunk in stream_generate(model, processor, prompt, **media, **options):
                if job_id in cancelled:
                    finish = "cancelled"
                    break
                if chunk.text:
                    pieces.append(chunk.text)
                    send(event="token", id=job_id, text=chunk.text)
                last = chunk
            stats = {
                "seconds": round(time.time() - started, 2),
                "prompt_tokens": last.prompt_tokens if last else 0,
                "tokens": last.generation_tokens if last else 0,
                "tokens_per_sec": round(last.generation_tps, 1) if last else 0,
                "prompt_tokens_per_sec": round(last.prompt_tps, 1) if last else 0,
                "peak_memory_gb": round(mx.get_peak_memory() / 1e9, 2),
                "finish": finish or (last.finish_reason if last else None),
            }
            send(event="done", id=job_id, text="".join(pieces), stats=stats,
                 memory_gb=memory_gb())
            return

        # "audio": speech only (text to speech). "interleaved": speech with its text.
        # "auto": the model picks; it writes a tool call as text, or opens speech to answer.
        mode = {"audio": "audio", "interleaved": "interleaved", "auto": None}[job["output"]]
        stream = bool(job.get("stream"))
        inputs = prepare_inputs(processor, images=media["image"], audio=media["audio"], videos=media["video"],
                                prompts=prompt, add_special_tokens=add_special)
        input_ids = inputs.pop("input_ids")
        inputs.pop("attention_mask", None)
        tokens, frames, shown, sent, finish = [], [], "", 0, None
        with wired_limit(model, [generation_stream]), mx.stream(generation_stream):
            for token, codes in model.stream_slots(input_ids, generation_mode=mode, **options,
                                                   **{**model.audio_sampling, **inputs}):
                if job_id in cancelled:
                    finish = "cancelled"
                    break
                if codes is None:  # a text slot
                    tokens.append(token)
                    text = tokenizer.decode(tokens, skip_special_tokens=True)
                    if len(text) > len(shown) and text.startswith(shown) and not text.endswith("\ufffd"):
                        send(event="token", id=job_id, text=text[len(shown):])
                        shown = text
                    continue
                if codes[0].item() == config.audio_eos_code:
                    continue
                frames.append(codes)
                ready = len(frames) - 1 - sent  # the newest frame waits for the next one
                if stream and ready >= (FIRST_PIECE if sent == 0 else PIECE):
                    piece = decode_piece(frames, sent, len(frames) - 1)
                    send(event="audio_chunk", id=job_id, rate=rate, pcm=base64.b64encode(pcm16(piece)).decode())
                    sent = len(frames) - 1
            if stream and frames and sent < len(frames):
                piece = decode_piece(frames, sent, len(frames))
                send(event="audio_chunk", id=job_id, rate=rate, pcm=base64.b64encode(pcm16(piece)).decode())
            full = codec.decode(mx.stack(frames, axis=-1)) if frames else None
            if full is not None:
                mx.eval(full)
        text = tokenizer.decode(tokens, skip_special_tokens=True)
        out_path = outputs / f"{job_id}.wav"
        audio_seconds = 0.0
        if full is not None and full.size:
            save_wav(out_path, full)
            audio_seconds = round(full.size / rate, 2)
        slots = len(tokens) + len(frames)
        elapsed = time.time() - started
        stats = {
            "seconds": round(elapsed, 2),
            "prompt_tokens": int(np.asarray(input_ids).size),
            "tokens": slots,
            "audio_seconds": audio_seconds,
            "peak_memory_gb": round(mx.get_peak_memory() / 1e9, 2),
            "finish": finish or ("stop" if tokens and tokens[-1] in stop_ids or frames else "length"),
        }
        send(event="done", id=job_id, text=text.strip(), stats=stats,
             audio=out_path.name if audio_seconds else None, memory_gb=memory_gb())

    while (job := jobs.get()) is not None:
        try:
            run(job)
        except Exception as exc:
            log(traceback.format_exc())
            send(event="error", id=job.get("id"), message=f"Generation failed: {exc}")
        finally:
            cancelled.discard(job.get("id"))
            mx.clear_cache()
    return 0


if __name__ == "__main__":
    sys.exit(main())

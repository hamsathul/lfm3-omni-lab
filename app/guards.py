"""Guards around generation: the context limit, and speech that has turned into noise.

No side effects on import, so the worker, the server and the tests can all use it.
"""

import re

# Liquid's stated context for LFM3-Omni-2B, prompt and reply together. config.json says
# 128,000 positions, but the model card says to plan around 32K.
CONTEXT_TOKENS = 32768
MIN_REPLY_TOKENS = 64


class PromptTooLong(Exception):
    def __init__(self, prompt_tokens):
        super().__init__(f"The prompt is {prompt_tokens} tokens; the model's context is {CONTEXT_TOKENS}.")
        self.prompt_tokens = prompt_tokens


def reply_budget(prompt_tokens, wanted, least=MIN_REPLY_TOKENS):
    """Tokens left for the reply, or PromptTooLong when fewer than ``least`` are left."""
    room = CONTEXT_TOKENS - prompt_tokens
    if room < max(least, MIN_REPLY_TOKENS):
        raise PromptTooLong(prompt_tokens)
    return min(wanted, room)


class SpeechWatch:
    """Notices generated speech that has turned into continuous noise.

    Now and then the model loses its place, most often in the speech that runs on after an
    interleaved reply's text has ended, and goes on making rumble or a held vowel until it
    hits the token limit. Speech has gaps between words, so within any two seconds its level
    swings widely: at least 19 dB in 184 good replies. The noise swung 3-10 dB. Two windows
    in a row under 12 dB stop the reply, and the speech is cut where the noise began.
    """

    WINDOW_SECONDS, QUIET_SPREAD_DB, WINDOWS_IN_A_ROW = 2.0, 12.0, 2

    def __init__(self, rate, np=None):
        if np is None:
            import numpy as np
        self.np, self.hop = np, int(0.02 * rate)  # 20 ms loudness steps
        self.per_window = int(self.WINDOW_SECONDS / 0.02)
        self.rest, self.levels, self.checked, self.flat = np.zeros(0, np.float32), [], 0, 0

    def feed(self, samples):
        """Add decoded samples; returns the second where the noise began, once it's sure."""
        np = self.np
        audio = np.concatenate([self.rest, np.asarray(samples, np.float32)])
        whole = len(audio) // self.hop * self.hop
        steps = audio[:whole].reshape(-1, self.hop)
        self.levels += (10 * np.log10((steps ** 2).mean(axis=1) + 1e-10)).tolist()
        self.rest = audio[whole:]
        while len(self.levels) >= (self.checked + 1) * self.per_window:
            window = self.levels[self.checked * self.per_window:(self.checked + 1) * self.per_window]
            spread = np.percentile(window, 90) - np.percentile(window, 10)
            self.checked += 1
            self.flat = self.flat + 1 if spread < self.QUIET_SPREAD_DB else 0
            if self.flat >= self.WINDOWS_IN_A_ROW:
                return (self.checked - self.flat) * self.WINDOW_SECONDS
        return None


def speakable_units(text):
    """Roughly the words a text takes to say: words, plus one per digit and CJK character."""
    return len(re.findall(r"[^\W\d_]+", text)) + len(re.findall(r"\d", text)) + len(re.findall(r"[\u3040-\u9fff\uac00-\ud7af]", text))

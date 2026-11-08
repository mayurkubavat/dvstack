#!/usr/bin/env python3
"""UserPromptSubmit: notice pasted sim logs or waveform screenshots.

Lauren Tan's "human relay trap": the human runs the tool and pastes results back.
When a prompt looks like a pasted log or a screenshot of a waveform, suggest the
agent run control-eda itself next time, so the loop closes without the human.
"""
from __future__ import annotations

import re
import sys

sys.path.insert(0, __file__.rsplit("/", 1)[0])
import dvhook  # noqa: E402

LOG_RE = re.compile(r"(UVM_(ERROR|FATAL|INFO)|%Error|\$finish|TEST (PASSED|FAILED)|Assertion failed|@\s*\d+\s*ns)", re.I)


def main() -> None:
    ev = dvhook.read_event()
    prompt = ev.get("prompt") or ""
    hits = len(LOG_RE.findall(prompt))
    mentions_wave = bool(re.search(r"(waveform|screenshot|verdi|gtkwave|\.fsdb|\.vcd|\.fst)", prompt, re.I))
    if hits >= 3 or (mentions_wave and len(prompt) < 400):
        dvhook.allow(
            "[dv:human-relay] This prompt looks like a pasted simulation log or a waveform hand-off. "
            "Reproduce it yourself instead of working from the paste: `control-eda sim --test <t> --seed <s> --trace --json` "
            "then `control-eda wave-query <dump> --at <time> --window 20 --signals '<dut>.*' --json`. "
            "Use the pasted text only to find the test, seed and time."
        )
    dvhook.allow()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        dvhook.warn_and_allow(f"human_relay_check: {e}")

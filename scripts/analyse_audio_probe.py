"""Experiment 6: can the model's keep-alive padding be told from its speech?

The drain added in PR #14 removes non-speech from the playout queue so queue
depth stops being latency. It did not work on a real call. On 2026-10-06 the
OUT queue was 95% padding by volume - 667 source characters, about 40 s of
speech in a 683 s call - and still pinned at the 30 s lag cap, with the drain
removing nothing at all.

The reason is a lab measurement that did not transfer. Experiment 2 measured
the model's idle output peaking at 1078, from a session fed prerecorded audio
already in the target language, and SPEECH_PEAK = 2000 was set above it. On a
live call the leading non-quiet run in the queue has a median length of 0.10 s,
which means the keep-alive stream crosses 2000 roughly every 100 ms. Every
quiet run is then one or two frames, and a drain that needs a 200 ms run -
because a dip inside a word is one or two frames - correctly declines all of
them.

So the question is no longer how much padding there is. It is whether padding
is separable from speech at all, and by what signal. This reads the per-frame
energies recorded by `--probe-audio` on a real call and answers:

  1. How do frame energies distribute when the model is speaking versus idle,
     where "speaking" is its OWN signal - a recent output transcription event
     - and not an amplitude guess.
  2. Whether any single threshold separates them, and at what error rate.
  3. How long the quiet runs are at each candidate threshold, which is what
     decides whether MIN_DRAIN_RUN_MS can be satisfied.
  4. What chunk sizes the model sends - never measured, and it decides
     whether dropping whole chunks is viable instead of frame surgery.
  5. Whether a chunk is ever entirely padding, which is the safest disposal
     rule available: a chunk carrying any speech is kept whole.

Usage:
    uv run python scripts/analyse_audio_probe.py transcripts/<session>.audio-probe.jsonl

Reads energies and timings only. No audio, no transcript text.
"""

from __future__ import annotations

import json
import sys
from collections import Counter

# Seconds since the last output transcription within which the model is taken
# to be speaking. Generous: the transcription lags the audio it describes, so
# a tight window would label real speech as idle and poison the comparison.
SPEAKING_WINDOW_S = 1.5

CANDIDATE_THRESHOLDS = (300, 500, 800, 1078, 1500, 2000, 3000, 5000)


def load(path):
    entries = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                entries.append(json.loads(line))
    return entries


def percentiles(values, points=(1, 10, 25, 50, 75, 90, 99)):
    if not values:
        return {}
    ordered = sorted(values)
    return {
        p: ordered[min(len(ordered) - 1, int(len(ordered) * p / 100))] for p in points
    }


# Frames a quiet run must reach before the drain may touch it, mirroring
# playout.MIN_DRAIN_RUN_FRAMES. Below this a run cannot be told from the dip
# inside a word.
MIN_RUN_FRAMES = 10


def drainable(sequence, threshold, min_run=MIN_RUN_FRAMES):
    """Frames the drain could remove, and how many of them were speech.

    Computed over the WHOLE ordered stream, never per chunk: a run crossing a
    chunk boundary is still one run, and with 100 ms chunks a per-chunk scan
    can never see a 200 ms run at all - it would report zero for every
    threshold and look like proof that nothing is drainable.

    `sequence` is [(peak, was_speaking)] in delivery order.
    """
    removable = speech_lost = 0
    run: list[bool] = []
    for peak, speaking in sequence + [(1 << 30, False)]:
        if peak < threshold:
            run.append(speaking)
            continue
        if len(run) >= min_run:
            removable += len(run)
            speech_lost += sum(run)
        run = []
    return removable, speech_lost


def report(entries, direction):
    rows = [e for e in entries if e["d"] == direction]
    if not rows:
        print(f"\n### {direction.upper()}: nothing recorded")
        return
    speaking, idle = [], []
    speaking_chunks, idle_chunks = [], []
    sequence = []
    for row in rows:
        recent = row["tt"] is not None and row["tt"] <= SPEAKING_WINDOW_S
        (speaking if recent else idle).extend(row["peaks"])
        (speaking_chunks if recent else idle_chunks).append(row)
        sequence.extend((peak, recent) for peak in row["peaks"])

    total = len(speaking) + len(idle)
    print(f"\n### {direction.upper()}")
    print(f"chunks {len(rows)}, frames {total} = {total * 0.02:.0f}s of output audio")
    print(f"  while speaking (target text within {SPEAKING_WINDOW_S}s): "
          f"{len(speaking)} frames ({len(speaking)/total*100:.0f}%)")
    print(f"  while idle:                                    "
          f"{len(idle)} frames ({len(idle)/total*100:.0f}%)")

    print("\n  frame peak percentiles")
    print(f"    {'':10} " + " ".join(f"{f'p{p}':>7}" for p in (1, 10, 25, 50, 75, 90, 99)))
    for label, values in (("speaking", speaking), ("idle", idle)):
        pcts = percentiles(values)
        if pcts:
            print(f"    {label:10} " + " ".join(f"{pcts[p]:>7}" for p in sorted(pcts)))

    print("\n  what a frame-energy drain could actually remove")
    print(f"    {'thresh':>7} {'idle<thr':>9} {'speech<thr':>11} "
          f"{'drainable':>10} {'of which speech':>16}")
    for threshold in CANDIDATE_THRESHOLDS:
        idle_below = sum(1 for p in idle if p < threshold) / max(len(idle), 1)
        speech_below = sum(1 for p in speaking if p < threshold) / max(len(speaking), 1)
        removable, speech_lost = drainable(sequence, threshold)
        share = removable / max(total, 1)
        bad = speech_lost / max(removable, 1)
        print(f"    {threshold:>7} {idle_below*100:>8.1f}% {speech_below*100:>10.1f}% "
              f"{share*100:>9.1f}% {bad*100:>15.1f}%")
    print("    drainable = share of ALL frames sitting in a run of >=200ms below the")
    print("    threshold, measured across chunk boundaries. 'of which speech' is how")
    print("    much of that was really speech - the cost of acting at that threshold.")
    print(f"    Inflow needs ~{'30':>2}% removed to hold a 1.44x stream at target.")

    print("\n  chunk sizes the model sent")
    sizes = Counter(e["bytes"] for e in rows)
    for size, count in sizes.most_common(6):
        print(f"    {size:>7} bytes ({size/48000*1000:>6.1f} ms)  x{count}")
    partial = sum(1 for e in rows if e["partial"])
    print(f"    not a whole number of 20 ms frames: {partial} of {len(rows)}")

    print("\n  whole-chunk disposal (drop a chunk only if NOTHING in it is speech)")
    for threshold in (1078, 2000):
        silent_idle = [e for e in idle_chunks if e["peaks"] and max(e["peaks"]) < threshold]
        silent_speaking = [e for e in speaking_chunks
                           if e["peaks"] and max(e["peaks"]) < threshold]
        idle_bytes = sum(e["bytes"] for e in idle_chunks) or 1
        print(f"    threshold {threshold}: {len(silent_idle)} of {len(idle_chunks)} idle "
              f"chunks entirely below it "
              f"= {sum(e['bytes'] for e in silent_idle)/idle_bytes*100:.0f}% of idle bytes"
              f"; {len(silent_speaking)} speaking chunks would be lost")


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 2
    entries = load(argv[1])
    if not entries:
        print("no entries recorded")
        return 1
    span = entries[-1]["t"] - entries[0]["t"]
    print(f"# {argv[1]}")
    print(f"{len(entries)} chunks spanning {span:.0f}s of output audio")
    for direction in ("in", "out"):
        report(entries, direction)
    print("\nWhat to conclude:")
    print("  - A threshold with HIGH 'idle below' and LOW 'speech below' and a")
    print("    high last column means a frame-energy drain can work; retune")
    print("    SPEECH_PEAK's drain-side equivalent to it.")
    print("  - If no row achieves that, energy cannot separate them. Then the")
    print("    whole-chunk rule above, or gating on target-text timing, is the")
    print("    only safe disposal - and if neither covers the volume, the")
    print("    remaining levers are a lower --lag-cap or time-stretching.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

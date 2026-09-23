"""Experiment 3: does this model send GoAway, and can a session be resumed?

Streams silence for 12 minutes - past the documented ~10 minute connection
cap - with session resumption and context compression enabled, logging every
non-audio message with a timestamp.

Three questions:
  1. Does a `go_away` message arrive, and does it carry `time_left`?
  2. Do `session_resumption_update` messages arrive with handles?
  3. Does reconnecting with the last handle work?

Corrected against google-genai 2.24.0 (see docs/experiments/03-session-limits.md):
`session_resumption` and `context_window_compression` are top-level fields of
`LiveConnectConfig`, verified via `types.LiveConnectConfig.model_fields`
alongside the already-established `translation_config` /
`input_audio_transcription` / `output_audio_transcription` shape from
experiment 1. `send_realtime_input(audio=types.Blob(...))` was verified via
`inspect.signature` against `AsyncSession.send_realtime_input` - the plan's
call shape was correct as written.

Run (in background - this takes ~13 minutes):
  GEMINI_API_KEY=... uv run python scripts/exp03_session_limits.py
"""

import asyncio
import os
import time

from google import genai
from google.genai import types

MODEL = "gemini-3.5-live-translate-preview"

BLOCK = 3200  # 100 ms of 16 kHz s16 mono
SILENCE = b"\x00" * BLOCK
RUN_S = 12 * 60
RESUME_RUN_S = 60


def config(
    target: str = "ru",
    session_resumption_handle: str | None = None,
) -> types.LiveConnectConfig:
    return types.LiveConnectConfig(
        response_modalities=["AUDIO"],
        translation_config=types.TranslationConfig(
            target_language_code=target,
            echo_target_language=False,
        ),
        input_audio_transcription=types.AudioTranscriptionConfig(),
        output_audio_transcription=types.AudioTranscriptionConfig(),
        session_resumption=types.SessionResumptionConfig(
            handle=session_resumption_handle,
        ),
        context_window_compression=types.ContextWindowCompressionConfig(
            sliding_window=types.SlidingWindow(),
        ),
    )


def elapsed_prefix(start: float) -> str:
    return f"[t={time.monotonic() - start:6.1f}s]"


async def sender(session, start: float, run_s: float, stop: asyncio.Event) -> None:
    """Send 100ms silence blocks every 0.1s until stop is set or run_s elapses."""
    try:
        while not stop.is_set():
            if time.monotonic() - start >= run_s:
                break
            await session.send_realtime_input(
                audio=types.Blob(data=SILENCE, mime_type="audio/pcm;rate=16000")
            )
            await asyncio.sleep(0.1)
    except Exception as e:  # noqa: BLE001 - report, don't hide, connection errors
        print(f"{elapsed_prefix(start)} sender stopped: {e!r}", flush=True)
    finally:
        stop.set()


async def receiver(session, start: float, run_s: float, stop: asyncio.Event) -> dict:
    """Consume session.receive(), log every non-audio message.

    IMPORTANT: `session.receive()` is an async generator that ends when one
    "interaction"/turn completes (see google.genai.live.AsyncSession.receive) -
    it does NOT stay open for the life of the connection. So this polls
    `receive()` in an outer loop, re-invoking it whenever the inner generator
    ends early, and uses `asyncio.wait_for` on each `__anext__()` so it can
    check the deadline/stop event instead of blocking forever on a message
    that may never come (verified against the real cap: it never blocked for
    more than a message's real inter-arrival time, but this bounds it either
    way).

    Returns a dict with the last resumption handle seen (if any), the reason
    the loop ended, counts of interesting messages, and the last go_away seen.
    """
    result = {
        "last_handle": None,
        "last_resumable": None,
        "resumption_update_count": 0,
        "go_away_seen": False,
        "go_away_time_left": None,
        "go_away_elapsed_s": None,
        "unexpected_data_count": 0,
        "unexpected_data_bytes": 0,
        "end_reason": "unknown",
    }
    deadline = start + run_s
    last_data_summary_t = start

    def process(msg) -> None:
        nonlocal last_data_summary_t
        go_away = getattr(msg, "go_away", None)
        if go_away is not None:
            result["go_away_seen"] = True
            result["go_away_time_left"] = go_away.time_left
            result["go_away_elapsed_s"] = time.monotonic() - start
            print(
                f"{elapsed_prefix(start)} go_away: time_left="
                f"{go_away.time_left!r} (type={type(go_away.time_left).__name__})",
                flush=True,
            )

        resumption_update = getattr(msg, "session_resumption_update", None)
        if resumption_update is not None:
            result["resumption_update_count"] += 1
            if resumption_update.new_handle:
                result["last_handle"] = resumption_update.new_handle
            result["last_resumable"] = resumption_update.resumable
            print(
                f"{elapsed_prefix(start)} session_resumption_update #{result['resumption_update_count']}: "
                f"new_handle={resumption_update.new_handle!r} "
                f"resumable={resumption_update.resumable!r}",
                flush=True,
            )

        data = getattr(msg, "data", None)
        if data:
            result["unexpected_data_count"] += 1
            result["unexpected_data_bytes"] += len(data)
            now = time.monotonic()
            # Throttle: this is a side observation, not one of the two
            # headline signals, and silence produces it continuously. Print
            # the first occurrence, then a running tally at most every 30s.
            if result["unexpected_data_count"] == 1 or now - last_data_summary_t >= 30:
                print(
                    f"{elapsed_prefix(start)} UNEXPECTED: audio data present despite "
                    f"silence input (count so far={result['unexpected_data_count']}, "
                    f"bytes so far={result['unexpected_data_bytes']})",
                    flush=True,
                )
                last_data_summary_t = now

        server_content = getattr(msg, "server_content", None)
        if server_content is not None and go_away is None:
            interrupted = getattr(server_content, "interrupted", None)
            turn_complete = getattr(server_content, "turn_complete", None)
            if interrupted or turn_complete:
                print(
                    f"{elapsed_prefix(start)} server_content: "
                    f"interrupted={interrupted!r} turn_complete={turn_complete!r}",
                    flush=True,
                )

    try:
        while True:
            if stop.is_set():
                result["end_reason"] = "stop event set (sender ended or deadline reached)"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                result["end_reason"] = "reached run_s deadline"
                break

            gen = session.receive()
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or stop.is_set():
                        break
                    try:
                        msg = await asyncio.wait_for(
                            gen.__anext__(), timeout=min(remaining, 1.0)
                        )
                    except asyncio.TimeoutError:
                        continue
                    except StopAsyncIteration:
                        # One interaction/turn ended; loop the outer while to
                        # call session.receive() again and keep listening.
                        break
                    process(msg)
            finally:
                aclose = getattr(gen, "aclose", None)
                if aclose is not None:
                    try:
                        await aclose()
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        pass
    except Exception as e:  # noqa: BLE001 - the whole point is to see how it ends
        result["end_reason"] = f"exception: {e!r}"
        print(f"{elapsed_prefix(start)} receiver ended with exception: {e!r}", flush=True)
    finally:
        if result["end_reason"] == "unknown":
            result["end_reason"] = "loop exited without a recorded reason (bug?)"
        print(
            f"{elapsed_prefix(start)} receiver final tally: "
            f"unexpected_data_count={result['unexpected_data_count']} "
            f"unexpected_data_bytes={result['unexpected_data_bytes']}",
            flush=True,
        )
        stop.set()
    return result


async def run_first_connection(client: genai.Client) -> dict:
    print(f"=== Phase 1: primary connection, target {RUN_S}s ===", flush=True)
    start = time.monotonic()
    stop = asyncio.Event()
    connect_started = time.monotonic()
    try:
        async with client.aio.live.connect(model=MODEL, config=config()) as session:
            connected_ms = (time.monotonic() - connect_started) * 1000
            print(f"connected in {connected_ms:.0f} ms", flush=True)
            send_task = asyncio.create_task(sender(session, start, RUN_S, stop))
            recv_task = asyncio.create_task(receiver(session, start, RUN_S, stop))

            # Both tasks watch `stop`/the deadline themselves; just wait for
            # them to finish (whichever ends first sets `stop`).
            await send_task
            recv_result = await recv_task
    except Exception as e:  # noqa: BLE001 - connection-level failure is data too
        print(f"{elapsed_prefix(start)} connection-level exception: {e!r}", flush=True)
        recv_result = {
            "last_handle": None,
            "last_resumable": None,
            "resumption_update_count": 0,
            "go_away_seen": False,
            "go_away_time_left": None,
            "go_away_elapsed_s": None,
            "unexpected_data_count": 0,
            "unexpected_data_bytes": 0,
            "end_reason": f"connection exception: {e!r}",
        }

    total_elapsed = time.monotonic() - start
    recv_result["connection_duration_s"] = total_elapsed
    print(
        f"=== Phase 1 done: lasted {total_elapsed:.1f}s, "
        f"end_reason={recv_result['end_reason']!r} ===",
        flush=True,
    )
    return recv_result


async def run_resume_check(client: genai.Client, handle: str) -> None:
    print(f"=== Phase 2: reconnect with handle, target {RESUME_RUN_S}s ===", flush=True)
    start = time.monotonic()
    stop = asyncio.Event()
    connect_started = time.monotonic()
    try:
        async with client.aio.live.connect(
            model=MODEL, config=config(session_resumption_handle=handle)
        ) as session:
            connected_ms = (time.monotonic() - connect_started) * 1000
            print(f"resumed connection established in {connected_ms:.0f} ms", flush=True)
            send_task = asyncio.create_task(sender(session, start, RESUME_RUN_S, stop))
            recv_task = asyncio.create_task(receiver(session, start, RESUME_RUN_S, stop))

            await send_task
            await recv_task
        print("=== Phase 2 done: resumption reconnect succeeded ===", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"=== Phase 2 FAILED: {e!r} ===", flush=True)


async def main() -> None:
    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    result = await run_first_connection(client)

    print(flush=True)
    print("=== Summary of Phase 1 ===", flush=True)
    print(f"connection_duration_s: {result['connection_duration_s']:.1f}", flush=True)
    print(f"end_reason: {result['end_reason']}", flush=True)
    print(f"go_away_seen: {result['go_away_seen']}", flush=True)
    print(f"go_away_elapsed_s: {result['go_away_elapsed_s']}", flush=True)
    print(f"go_away_time_left: {result['go_away_time_left']!r}", flush=True)
    print(f"resumption_update_count: {result['resumption_update_count']}", flush=True)
    print(f"last_handle: {result['last_handle']!r}", flush=True)
    print(f"last_resumable: {result['last_resumable']!r}", flush=True)
    print(f"unexpected_data_count: {result['unexpected_data_count']}", flush=True)
    print(f"unexpected_data_bytes: {result['unexpected_data_bytes']}", flush=True)
    print(flush=True)

    if result["last_handle"]:
        await run_resume_check(client, result["last_handle"])
    else:
        print("=== Phase 2 skipped: no resumption handle obtained ===", flush=True)


if __name__ == "__main__":
    asyncio.run(main())

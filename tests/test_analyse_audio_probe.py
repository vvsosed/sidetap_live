"""The analyser reads chunk records; a playout record has no peaks."""

import importlib.util
import json
from pathlib import Path


def load_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "analyse_audio_probe.py"
    spec = importlib.util.spec_from_file_location("analyse_audio_probe", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_playout_records_are_skipped_not_crashed_on(tmp_path):
    script = load_script()
    path = tmp_path / "probe.jsonl"
    chunk = {"t": 0.0, "w": 0.0, "d": "in", "bytes": 960, "peaks": [100], "partial": False, "tt": None}
    playout = {"k": "playout", "w": 1.0, "d": "in", "backlog": 0.5, "written": 50,
               "duck": True, "blocked": 0.0, "duck_tr": 1, "duck_fail": 0}
    path.write_text(json.dumps(chunk) + "\n" + json.dumps(playout) + "\n", encoding="utf-8")

    entries = script.load(path)
    assert entries == [chunk]

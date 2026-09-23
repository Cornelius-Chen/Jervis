"""The public composition replay keeps independent work and rejects a late result."""
import json
from pathlib import Path
import subprocess
import sys
import wave


ROOT = Path(__file__).resolve().parents[1]


def test_composition_replay_from_empty_directory(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "run_composition_cycle.py"),
         "--output", str(tmp_path / "composition")],
        check=True, capture_output=True, text=True,
    )
    report = json.loads(Path(json.loads(result.stdout)["result"]).read_text(encoding="utf-8"))
    assert report["impact"]["definite"] == ["sound"]
    assert report["impact"]["propagated"] == ["observe", "page", "sound"]
    assert report["unchanged"] == ["motion", "notes"]
    assert set(report["rebuilt"]) == {"sound", "page", "observe"}
    assert report["late_commit_status"] == "STALE"
    assert report["human_quality_status"] == "AWAITING_HUMAN_EVIDENCE"
    with wave.open(report["old_wav"], "rb") as old, wave.open(report["new_wav"], "rb") as new:
        assert old.getframerate() == new.getframerate() == 22050
        assert old.readframes(old.getnframes()) != new.readframes(new.getnframes())
    for phase in ("before", "after"):
        ref = report[phase]["observe"]["result_ref"]
        version = ref.split(":")[-3]
        attempt = ref.split(":")[-2]
        observation = tmp_path / "composition" / "projects" / "pip-composition" / "work" / "observe" / f"{version}-{attempt}" / "composition-observation.json"
        assert json.loads(observation.read_text(encoding="utf-8"))["passed"]

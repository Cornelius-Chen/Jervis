"""The public entry point must reach real storage and browser observations."""
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def test_designer_cycle_from_clean_output(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "examples" / "run_designer_cycle.py"),
         "--output", str(tmp_path / "run")],
        check=True, capture_output=True, text=True,
    )
    receipt = json.loads(result.stdout)
    saved = json.loads(Path(receipt["result"]).read_text(encoding="utf-8"))
    assert saved["study"]["decision"]["status"] == "experimental"
    assert all(item["passed"] for item in saved["study"]["practice_checks"])
    assert saved["new_task_selected"]["selected_judgments"] == ["choice-action-group"]
    assert saved["new_task_rejected"]["selected_judgments"] == []
    for key in ("new_task_selected", "new_task_rejected"):
        case = saved[key]
        observation = json.loads(Path(case["observation"]).read_text(encoding="utf-8"))
        assert case["workflow_passed"]
        assert observation["visual_review"]["human_effect"] == "AWAITING_HUMAN_EVIDENCE"
        assert Path(case["page"]).is_file()
        assert all(Path(path).is_file() for path in observation["screenshots"])

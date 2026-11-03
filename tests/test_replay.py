"""The stage-by-stage replay, on a scripted run. No network, no API key."""

import re

from eval.run_eval import GOLD, run_all
from triage.config import load_settings
from triage.replay import main
from triage.run import load_cases
from triage.schemas import Priority

from .conftest import AGREE, ScriptedAgents, disagree, make_triage

CASES = load_cases(GOLD)[:1]


def _run(tmp_path):
    scripted = lambda spec: ScriptedAgents(  # noqa: E731
        make_triage(), challenges=[disagree(), AGREE], revisions=[make_triage(priority=Priority.P2)]
    )
    run_all(["F"], CASES, tmp_path, 5.0, load_settings(), scripted, lambda _: None)
    return tmp_path / "F", CASES[0].case.case_id


def test_replay_prints_every_stage_the_case_went_through(tmp_path, capsys):
    run_dir, cid = _run(tmp_path)
    assert main([str(run_dir), cid]) == 0
    out = capsys.readouterr().out
    for heading in ("arrives", "Triage agent", "Challenger", "Triage revises, once", "Challenger re-checks", "Where it lands"):
        assert heading in out
    assert "priority: P3 → P2" in out


def test_frames_are_one_per_stage_and_the_same_size(tmp_path):
    run_dir, cid = _run(tmp_path)
    assert main([str(run_dir), cid, "--frames", str(tmp_path / "frames")]) == 0
    svgs = sorted((tmp_path / "frames").glob("*.svg"))
    assert [p.stem.split("-", 1)[1] for p in svgs] == ["case", "triage", "challenge", "revise", "recheck", "route"]
    sizes = {re.search(r'viewBox="([^"]+)"', p.read_text()).group(1) for p in svgs}
    assert len(sizes) == 1  # a GIF built from them won't jump


def test_unknown_case_is_refused(tmp_path):
    run_dir, _ = _run(tmp_path)
    assert main([str(run_dir), "NG-999"]) == 2

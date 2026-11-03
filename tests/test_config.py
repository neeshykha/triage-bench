from pathlib import Path

import pytest
from pydantic import ValidationError

from triage.config import DEFAULT_PATH, load_settings
from triage.orchestrator import Pipeline


def test_shipped_config_loads_and_defines_the_four_pipelines():
    settings = load_settings()
    assert set("ABCD") <= settings.pipelines.keys()
    assert settings.max_revisions == 1
    d = Pipeline.from_settings("D", settings)
    assert d.rules is not None and d.challenger and d.max_revisions == 1
    a = Pipeline.from_settings("A", settings)
    assert a.rules is None and not a.challenger


def test_a_model_without_a_price_fails_at_load(tmp_path: Path):
    text = DEFAULT_PATH.read_text().replace("model: claude-opus-5-5", "model: claude-mystery-9", 1)
    bad = tmp_path / "config.yaml"
    bad.write_text(text)
    with pytest.raises(ValidationError, match="claude-mystery-9"):
        load_settings(bad)

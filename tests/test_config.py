from pathlib import Path

import pytest
import yaml

from agent import config
from tests.conftest import EXAMPLES


@pytest.mark.parametrize(
    "loader, name",
    [
        (config.load_profile, "profile"),
        (config.load_settings, "settings"),
        (config.load_search, "search"),
        (config.load_companies, "companies"),
    ],
)
def test_examples_validate(loader, name):
    loader(EXAMPLES / f"{name}.example.yaml")


def _write(tmp_path: Path, data) -> Path:
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _example():
    return yaml.safe_load((EXAMPLES / "profile.example.yaml").read_text(encoding="utf-8"))


def test_unknown_key_fails(tmp_path):
    data = _example()
    data["contact"]["nickname"] = "x"
    with pytest.raises(config.ConfigError, match="nickname"):
        config.load_profile(_write(tmp_path, data))


def test_duplicate_bullet_id_fails(tmp_path):
    data = _example()
    data["projects"][0]["bullets"][0]["id"] = "example-corp-01"
    with pytest.raises(config.ConfigError, match="duplicate bullet ids"):
        config.load_profile(_write(tmp_path, data))


def test_todo_fails_with_its_path(tmp_path):
    data = _example()
    data["notice_period_days"] = "TODO"
    with pytest.raises(config.ConfigError, match="notice_period_days"):
        config.load_profile(_write(tmp_path, data))


def test_missing_file_has_hint(tmp_path):
    with pytest.raises(config.ConfigError, match="copy profile.example.yaml"):
        config.load_profile(tmp_path / "profile.yaml")


def test_zero_budget_requires_free_only(tmp_path):
    data = yaml.safe_load((EXAMPLES / "settings.example.yaml").read_text(encoding="utf-8"))
    data["llm"]["free_only"] = False
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(config.ConfigError, match="free_only"):
        config.load_settings(path)


def test_every_model_needs_limits(tmp_path):
    data = yaml.safe_load((EXAMPLES / "settings.example.yaml").read_text(encoding="utf-8"))
    data["llm"]["fast_models"].append("gemini-unknown")
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    with pytest.raises(config.ConfigError, match="gemini-unknown"):
        config.load_settings(path)

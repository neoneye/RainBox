"""env_file.load_env_file — the repo-root .env fallback layer."""

import os

from env_file import ENV_PATH, REPO_ROOT, load_env_file


def test_env_path_sits_at_the_repo_root():
    """Next to .env.example and .gitignore, one level above source/."""
    assert ENV_PATH == REPO_ROOT / ".env"
    assert (REPO_ROOT / ".env.example").exists()


def test_missing_file_is_not_an_error(tmp_path):
    assert load_env_file(tmp_path / "nope.env") is None


def test_values_are_loaded(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("PP3_ENVFILE_TEST=from-file\n")
    monkeypatch.delenv("PP3_ENVFILE_TEST", raising=False)
    assert load_env_file(path) == path
    assert os.environ["PP3_ENVFILE_TEST"] == "from-file"


def test_the_real_environment_wins(tmp_path, monkeypatch):
    """So `OPENROUTER_API_KEY=… python main.py` still overrides the file, and a
    test that sets the variable can't be clobbered by the operator's real key."""
    path = tmp_path / ".env"
    path.write_text("PP3_ENVFILE_TEST=from-file\n")
    monkeypatch.setenv("PP3_ENVFILE_TEST", "from-shell")
    load_env_file(path)
    assert os.environ["PP3_ENVFILE_TEST"] == "from-shell"

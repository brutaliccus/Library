"""VAPID bootstrap shell must close the python heredoc before docker compose."""

from app.services.setup_bootstrap import _vapid_env_write_script


def test_vapid_heredoc_terminator_is_alone_on_line():
    script = _vapid_env_write_script()
    # Closing delimiter must be its own line; "PY; docker ..." breaks python.
    assert "\nPY\n" in script
    assert "PY;" not in script
    assert script.index("\nPY\n") < script.index("docker compose up")

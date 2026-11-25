import pytest


@pytest.fixture
def home(tmp_path):
    """An environment whose state home is a fresh directory outside any repository."""
    return {"AI_HARNESS_HOME": str(tmp_path / "state"), "PATH": "/usr/bin:/bin"}

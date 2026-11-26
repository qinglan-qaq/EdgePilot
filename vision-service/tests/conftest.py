from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fakes import make_client, make_settings  # noqa: E402


@pytest.fixture
def client(tmp_path):
    with make_client(make_settings(tmp_path)) as test_client:
        yield test_client

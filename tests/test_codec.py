"""bootstrap sets the process-wide orjson codec."""

from datetime import datetime

import pytest
from stario import json


@pytest.mark.filterwarnings("error::DeprecationWarning")
async def test_bootstrap_sets_the_orjson_codec(yard):
    assert json.dumps({"a": "é"}) == '{"a":"é"}'
    assert json.loads(json.dumps_bytes({"a": "é"})) == {"a": "é"}
    # orjson serializes datetime natively; the stdlib codec raises TypeError.
    assert json.dumps_bytes({"at": datetime(2026, 1, 1)}) == b'{"at":"2026-01-01T00:00:00"}'

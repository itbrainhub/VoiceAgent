import json
import os
import tempfile
from types import SimpleNamespace

# Point the app at a throwaway SQLite file before anything imports app.db.
_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
os.environ["DATABASE_URL"] = f"sqlite:///{_tmp.name}"
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")

import pytest  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.db import Base, engine, init_db, session_scope  # noqa: E402
from app.models import MenuItem, Restaurant  # noqa: E402
from app.seed import DEMO_MENU, load_menu  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_db():
    Base.metadata.drop_all(engine)
    init_db()
    with session_scope() as s:
        load_menu(s, json.loads(DEMO_MENU.read_text()))
    yield


@pytest.fixture
def db():
    with session_scope() as s:
        yield s


@pytest.fixture
def restaurant(db) -> Restaurant:
    return db.scalar(select(Restaurant))


@pytest.fixture
def item_id(db):
    def _get(name: str) -> int:
        return db.scalar(select(MenuItem.id).where(MenuItem.name == name))
    return _get


# ---- a scripted stand-in for the Claude client --------------------------------

def text(t):
    return SimpleNamespace(type="text", text=t)


def tool(name, **inp):
    tool.n = getattr(tool, "n", 0) + 1
    return SimpleNamespace(type="tool_use", id=f"tu_{tool.n}", name=name, input=inp)


def reply(*blocks):
    stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
    return SimpleNamespace(content=list(blocks), stop_reason=stop)


class FakeClient:
    """Returns pre-scripted responses and records every request."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []
        self.messages = self

    async def create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot
        return self.responses.pop(0)

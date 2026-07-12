"""Shared in-memory fakes for the test suite."""

import json
from types import SimpleNamespace


class FakeJira:
    def __init__(self):
        self.properties: dict[tuple[str, str], object] = {}
        self.labels: dict[str, set[str]] = {}
        self.comments: dict[str, list[str]] = {}
        self.issues: dict[str, dict] = {}
        self.transitions: list[tuple[str, str]] = []
        self.get_issue_calls = 0

    async def myself(self):
        return {"name": "sentinel-bot"}

    async def get_property(self, key, prop):
        return self.properties.get((key, prop))

    async def set_property(self, key, prop, value):
        self.properties[(key, prop)] = value

    async def delete_property(self, key, prop):
        self.properties.pop((key, prop), None)

    async def update_labels(self, key, add=None, remove=None):
        labels = self.labels.setdefault(key, set())
        labels.update(add or [])
        labels.difference_update(remove or [])

    async def add_comment(self, key, body):
        self.comments.setdefault(key, []).append(body)
        return {"id": str(len(self.comments[key]))}

    async def get_comments(self, key):
        return [{"body": b} for b in self.comments.get(key, [])]

    async def assign(self, key, username):
        pass

    async def search(self, jql, max_results=100, fields=None):
        return []

    async def get_issue(self, key, with_comments=True):
        self.get_issue_calls += 1
        return self.issues.get(key, {"key": key, "fields": {
            "status": {"name": "In Progress"}, "labels": []}})

    async def transition_to(self, key, target_status):
        self.transitions.append((key, target_status))
        if key in self.issues:
            self.issues[key]["fields"]["status"]["name"] = target_status


def tool_call(call_id: str, name: str, args: dict) -> SimpleNamespace:
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def llm_msg(content: str | None = None, tool_calls: list | None = None) -> SimpleNamespace:
    return SimpleNamespace(content=content, tool_calls=tool_calls or None)


class FakeLLM:
    """Plays back a scripted sequence of chat responses.

    Script items may be message namespaces (from llm_msg) or exceptions to raise.
    When the script is exhausted, `default` is returned (if set) — useful for
    turn-cap tests — otherwise running out of script raises IndexError.
    """

    def __init__(self, script=(), default=None):
        self.script = list(script)
        self.default = default
        self.calls = 0

    async def chat(self, messages, tools=None, model=None, temperature=None):
        self.calls += 1
        if not self.script:
            if self.default is not None:
                return self.default
            raise IndexError("FakeLLM script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

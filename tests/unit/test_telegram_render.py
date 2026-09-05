from __future__ import annotations

from pathlib import Path

from protocore.runtime.events.envelope import TurnEvent
from protocore.runtime.events.types import EventType

from daedalus.transport.telegram.markdown import markdown_to_html, split_message
from daedalus.transport.telegram.render import RunRenderer, RunView


class FakeOutbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []
        self.edits: list[tuple[int, str]] = []
        self.documents: list[Path] = []

    async def send_text(self, text: str, *, markdown: bool = True) -> int:
        self.sent.append((text, markdown))
        return len(self.sent)

    async def edit_text(self, message_id: int, text: str) -> None:
        self.edits.append((message_id, text))

    async def send_document(self, path: Path, caption: str | None = None) -> int:
        self.documents.append(path)
        return 99

    async def delete(self, message_id: int) -> None:
        return None


def _evt(t: EventType, **payload):  # type: ignore[no-untyped-def]
    return TurnEvent(type=t, run_id="r1", payload=payload)


async def test_renderer_sends_final_answer_and_freezes_status(tmp_path: Path) -> None:
    outbox = FakeOutbox()
    renderer = RunRenderer(outbox, RunView(run_id="r1", model="m"), edit_interval=0.0)
    await renderer.handle(_evt(EventType.MESSAGE_START))
    await renderer.handle(_evt(EventType.TOOL_USE_START, tool_call_id="c1", tool_name="Exec"))
    await renderer.handle(_evt(EventType.TOOL_USE_STOP, tool_call_id="c1", final_input={"command": "ls"}))
    await renderer.handle(_evt(EventType.TOOL_RESULT, tool_call_id="c1", content="a\nb"))
    await renderer.handle(_evt(EventType.MESSAGE_STOP, stop_reason="tool_use", tokens_used={"total": 10}))
    await renderer.handle(_evt(EventType.MESSAGE_START))
    await renderer.handle(_evt(EventType.CONTENT_BLOCK_DELTA, delta={"type": "text_delta", "text": "All **done**"}))
    await renderer.handle(_evt(EventType.MESSAGE_STOP, stop_reason="end_turn", tokens_used={"total": 20}))
    await renderer.flush()
    await renderer.finish("completed", workspace=tmp_path)
    finals = [t for t, md in outbox.sent if md]
    assert finals == ["All **done**"]
    status_texts = [t for t, md in outbox.sent if not md] + [t for _, t in outbox.edits]
    assert any("Exec ls" in t for t in status_texts)
    assert outbox.edits[-1][1].startswith("✅ completed")


async def test_long_answer_becomes_a_document(tmp_path: Path) -> None:
    outbox = FakeOutbox()
    renderer = RunRenderer(outbox, RunView(run_id="r1", model="m"), edit_interval=0.0)
    await renderer.handle(_evt(EventType.MESSAGE_START))
    await renderer.handle(_evt(EventType.CONTENT_BLOCK_DELTA, delta={"type": "text_delta", "text": "x" * 20_000}))
    await renderer.handle(_evt(EventType.MESSAGE_STOP, stop_reason="end_turn"))
    await renderer.finish("completed", workspace=tmp_path)
    assert outbox.documents and outbox.documents[0].read_text() == "x" * 20_000


def test_split_keeps_code_fences_balanced() -> None:
    text = "intro\n```python\n" + "\n".join(f"line {i}" for i in range(800)) + "\n```\noutro"
    chunks = split_message(text, limit=2000)
    assert len(chunks) > 1
    for chunk in chunks:
        assert chunk.count("```") % 2 == 0
    assert "".join(c.replace("```python\n", "").replace("```\n", "") for c in chunks).count("line 799") == 1


def test_markdown_to_html_basics() -> None:
    html = markdown_to_html("# Title\n**bold** and `code` and [x](https://e.com)\n```sh\necho <hi>\n```")
    assert "<b>Title</b>" in html and "<b>bold</b>" in html and "<code>code</code>" in html
    assert '<a href="https://e.com">x</a>' in html
    assert '<pre><code class="language-sh">echo &lt;hi&gt;</code></pre>' in html

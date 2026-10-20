import shutil
from pathlib import Path

from panelcad import Session
from panelcad.api.activity import drain_inbox, drain_prompts, inbox_path, journal_path, send
from panelcad.cli.agent import Agent

ROOT = Path(__file__).resolve().parents[1]


def test_announce_and_journal(tmp_path: Path):
    s = Session.create("AI")
    dest = tmp_path / "job.pcad.yaml"
    s.save(dest)
    ev = s.announce(model="Grok 4.6", name="Grok")
    assert ev["op"] == "hello"
    assert ev["actor"]["model"] == "Grok 4.6"
    s.place("iec.operators.pb_no", ref="S1", x=40, y=40)
    log = journal_path(dest)
    assert log is not None and log.is_file()
    text = log.read_text(encoding="utf-8")
    assert "Grok 4.6" in text
    assert "place S1" in text


def test_agent_hello_say_idle():
    agent = Agent()
    r = agent.handle({"id": 1, "op": "hello", "model": "Grok 4.6"})
    assert r["ok"]
    assert r["result"]["actor"]["model"] == "Grok 4.6"
    r = agent.handle({"id": 2, "op": "say", "text": "packing enclosure"})
    assert r["ok"]
    assert r["result"]["summary"] == "packing enclosure"
    r = agent.handle({"id": 3, "op": "idle"})
    assert r["ok"]
    assert r["result"]["status"] == "idle"


def test_journal_follows_the_opened_copy_not_the_example(tmp_path: Path):
    src = ROOT / "examples" / "dol_starter.pcad.yaml"
    original = journal_path(src)
    assert original is not None
    before = original.read_bytes() if original.is_file() else None
    copy = tmp_path / src.name
    shutil.copy2(src, copy)
    copy_log = journal_path(copy)
    assert copy_log is not None
    copy_log.write_text('{"op":"seed"}\n', encoding="utf-8")

    session = Session.open(copy)
    session.number_wires()

    text = copy_log.read_text(encoding="utf-8")
    assert text.startswith('{"op":"seed"}\n')
    assert "number_wires" in text
    assert copy_log.parent == tmp_path
    if before is None:
        assert not original.exists()
    else:
        assert original.read_bytes() == before


def test_journal_follows_save_as(tmp_path: Path):
    first = tmp_path / "a.pcad.yaml"
    second = tmp_path / "nested" / "b.pcad.yaml"
    session = Session.create("Move")
    session.save(first)
    session.place("iec.operators.pb_no", ref="S1", x=10, y=10)
    first_log = journal_path(first)
    assert first_log is not None and first_log.is_file()
    assert "place S1" in first_log.read_text(encoding="utf-8")

    session.save(second)
    session.place("iec.operators.pb_no", ref="S2", x=30, y=10)
    second_log = journal_path(second)
    assert second_log is not None and second_log.is_file()
    second_text = second_log.read_text(encoding="utf-8")
    assert "place S2" in second_text
    assert "place S2" not in first_log.read_text(encoding="utf-8")


def test_inbox_drives_session(tmp_path: Path):
    dest = tmp_path / "live.pcad.yaml"
    s = Session.create("Live")
    s.save(dest)
    send(dest, {"id": 1, "op": "hello", "model": "Grok 4.6"})
    send(dest, {"id": 2, "op": "place", "library_id": "iec.operators.pb_no", "ref": "S1", "x": 50, "y": 40})
    send(dest, {"id": 3, "op": "say", "text": "placed start button"})
    agent = Agent()
    agent.session = s
    s.announce("Grok 4.6")
    replies = drain_inbox(dest, agent.handle)
    assert len(replies) == 3
    assert all(r.get("ok") for r in replies)
    assert s.sheet().component("S1").library_id == "iec.operators.pb_no"
    inbox = inbox_path(dest)
    assert inbox is not None
    again = drain_inbox(dest, agent.handle)
    assert again == []


def test_prompt_adds_ethernet_switch():
    from panelcad.examples.dol import build_dol

    s = build_dol()
    s.announce("Grok 4.6")
    result = s.run_prompt("add an ethernet switch to ethernet")
    assert result["ok"], result
    eth = s.sheet("ethernet")
    assert any(c.library_id == "iec.network.switch_5p" for c in eth.components)
    assert any("SW" in c.id for c in eth.components)


def test_prompt_line_queues_for_ai(tmp_path: Path):
    dest = tmp_path / "ask.pcad.yaml"
    s = Session.create("Ask")
    s.save(dest)
    s.submit_prompt("delete A3 and wire a start button to the CPU")
    rows = drain_prompts(dest)
    assert len(rows) == 1
    assert "start button" in rows[0]["text"]
    assert drain_prompts(dest) == []
    agent = Agent()
    agent.session = s
    r = agent.handle({"id": 1, "op": "list_prompts"})
    assert r["ok"]
    assert any("start button" in p.get("text", "") for p in r["result"]["prompts"])


def test_gui_ai_dock_shows_model_and_page():
    from PySide6.QtWidgets import QApplication

    from panelcad.gui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    win = MainWindow(Session.create("Visible"))
    win.show()
    app.processEvents()
    assert win.ai_wrap.windowTitle() == "AI"
    win.session.announce("Grok 4.6")
    win.session.emit("say", "drawing I/O", status="working")
    app.processEvents()
    assert "Grok 4.6" in win.ai_dock.banner.text() or "Grok 4.6" in win.ai_dock.meta.text()
    assert "working" in win.ai_dock.banner.text().lower() or "Grok 4.6" in win.ai_dock.banner.text()
    win.ai_wrap.hide()
    win.reset_tool_windows()
    app.processEvents()
    assert win.ai_wrap.isVisible()
    assert win.ai_dock.prompt is not None
    win.ai_dock.prompt.setText("add an estop on control")
    win.ai_dock._send_prompt()
    app.processEvents()
    texts = [win.ai_dock.log.item(i).text() for i in range(win.ai_dock.log.count())]
    assert any("estop" in t.lower() or "you:" in t.lower() for t in texts)
    win.close()

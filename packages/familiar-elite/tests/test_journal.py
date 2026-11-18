"""Journal startup, partial writes and session/file changes."""

import asyncio

from familiar_elite.journal import JournalTailer


def test_bootstrap_and_partial_lines(journal_dir):
    tail = JournalTailer(journal_dir)
    records = tail.poll()
    assert [r.data["event"] for r in records] == [
        "Fileheader",
        "LoadGame",
        "Location",
        "FutureEvent",
    ]
    assert all(r.historical for r in records)
    with tail.path.open("ab") as stream:
        stream.write(b'{"event":"FSDJump","StarSystem":"Achen')
    assert tail.poll() == []
    with tail.path.open("ab") as stream:
        stream.write(b'ar"}\n')
    (record,) = tail.poll()
    assert not record.historical and record.data["StarSystem"] == "Achenar"
    assert tail.poll() == []


def test_startup_partial_is_historical(tmp_path):
    path = tmp_path / "Journal.01.log"
    path.write_bytes(b'{"event":"Docked"')
    tail = JournalTailer(tmp_path)
    assert not tail.poll()
    with path.open("ab") as stream:
        stream.write(b'}\n{"event":"Undocked"}\n')
    assert [r.historical for r in tail.poll()] == [True, False]


def test_rotation_truncation_bad_lines(journal_dir, caplog):
    tail = JournalTailer(journal_dir)
    tail.poll()
    path = journal_dir / "Journal.20261117130000.01.log"
    path.write_bytes(b'\xef\xbb\xbf{"event":"LoadGame"}\n\xff\ninvalid\n[]\n')
    with caplog.at_level("DEBUG"):
        (record,) = tail.poll()
    assert record.data["event"] == "LoadGame" and not record.historical
    assert "undecodable" in caplog.text
    path.write_bytes(b'{"event":"Died"}\n')
    assert tail.poll()[0].data["event"] == "Died"


def test_late_directory_and_replaced_file(tmp_path):
    tail = JournalTailer(tmp_path)
    assert tail.poll() == []
    path = tmp_path / "Journal.01.log"
    path.write_text('{"event":"Docked"}\n')
    assert not tail.poll()[0].historical
    replacement = tmp_path / "replacement"
    replacement.write_text('{"event":"Undocked"}\n')
    replacement.replace(path)
    assert not tail.poll()[0].historical
    assert JournalTailer(None).poll() == []


async def test_async_tailer_and_close(journal_dir):
    tail = JournalTailer(journal_dir, 0.001)
    iterator = tail.__aiter__()
    assert (await asyncio.wait_for(anext(iterator), 1)).historical
    await tail.aclose()
    await iterator.aclose()

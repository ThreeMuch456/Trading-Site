"""Legacy history import: additive, idempotent, desk rows win.

Builds small legacy + live SQLite files and checks the three behaviours that
matter: session ids are remapped (not copied), an ungraded live session yields to
a graded legacy one while a graded live session is kept, and a second run over
the same file changes nothing.
"""
import json
import os
import sqlite3

from engines import legacy_import as li


def _agent_db(path, sessions):
    """sessions: [(date, gameplan, [(ticker, bias_correct)], review_or_None)]"""
    con = sqlite3.connect(path)
    con.executescript(li._AGENT_SCHEMA)
    for date, plan, outcomes, review in sessions:
        cur = con.execute(
            "INSERT INTO sessions (date, fetched_at, market_data, macro_data, options_data, gameplan) "
            "VALUES (?,?,?,?,?,?)", (date, "t", "{}", "{}", "{}", plan))
        for ticker, correct in outcomes:
            con.execute("INSERT INTO outcomes (session_id, ticker, day_pct, bias_correct) "
                        "VALUES (?,?,?,?)", (cur.lastrowid, ticker, 0.5, correct))
        if review:
            con.execute("INSERT INTO reviews (session_id, generated_at, data) VALUES (?,?,?)",
                        (cur.lastrowid, "t", review))
    con.commit()
    con.close()


def _rows(path, sql):
    con = sqlite3.connect(path)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_agent_import_remaps_replaces_and_is_idempotent(tmp_path):
    legacy, live = str(tmp_path / "journal.db"), str(tmp_path / "agent.sqlite3")
    _agent_db(legacy, [
        ("2026-11-10", "legacy-10", [("SPY", 1), ("QQQ", 0)], "rev-10"),
        ("2026-11-12", "legacy-12", [("SPY", 1)], None),
        ("2026-11-13", "legacy-13", [("SPY", 0)], "rev-13"),
        ("2026-11-14", "legacy-preview", [], None),          # after last graded -> skipped
    ])
    # Live journal: an unrelated earlier-id row, an UNGRADED 11-13 (yields to legacy)
    # and a GRADED 11-12 (kept).
    _agent_db(live, [
        ("2026-11-16", "desk-16", [], None),
        ("2026-11-13", "desk-13", [], None),
        ("2026-11-12", "desk-12", [("SPY", 0)], None),
    ])

    stats = li.import_agent(legacy, live)
    assert stats == {"sessions_added": 1, "sessions_replaced": 1, "sessions_kept": 1,
                     "previews_skipped": 1, "outcomes_added": 3, "reviews_added": 2}

    plans = dict(_rows(live, "SELECT date, gameplan FROM sessions"))
    assert plans == {"2026-11-16": "desk-16", "2026-11-13": "legacy-13",
                     "2026-11-12": "desk-12", "2026-11-10": "legacy-10"}
    # Outcomes and reviews follow their session by DATE, not by the legacy id.
    joined = _rows(live, "SELECT s.date, o.ticker, o.bias_correct FROM outcomes o "
                         "JOIN sessions s ON s.id = o.session_id ORDER BY s.date, o.ticker")
    assert joined == [("2026-11-10", "QQQ", 0), ("2026-11-10", "SPY", 1),
                      ("2026-11-12", "SPY", 0),          # the desk's own grade, kept
                      ("2026-11-13", "SPY", 0)]
    reviews = _rows(live, "SELECT s.date, r.data FROM reviews r "
                          "JOIN sessions s ON s.id = r.session_id ORDER BY s.date")
    assert reviews == [("2026-11-10", "rev-10"), ("2026-11-13", "rev-13")]

    again = li.import_agent(legacy, live)
    assert again["sessions_added"] == again["sessions_replaced"] == 0
    assert again["outcomes_added"] == again["reviews_added"] == 0
    assert len(_rows(live, "SELECT 1 FROM outcomes")) == 4


def test_merge_tables_is_additive_and_live_rows_win(tmp_path):
    legacy, live = str(tmp_path / "legacy.db"), str(tmp_path / "live.db")
    schema = ("CREATE TABLE touches (session_date TEXT, ticker TEXT, touch_id TEXT, "
              "payload TEXT, PRIMARY KEY (session_date, ticker, touch_id))")
    con = sqlite3.connect(legacy)
    con.execute(schema)
    con.execute("CREATE TABLE daily_option_volume (ticker TEXT, session_date TEXT, "
                "volume REAL, PRIMARY KEY (ticker, session_date))")
    con.executemany("INSERT INTO touches VALUES (?,?,?,?)",
                    [("2026-11-12", "SPY", "a", "legacy-a"), ("2026-11-13", "SPY", "b", "legacy-b")])
    con.execute("INSERT INTO daily_option_volume VALUES ('SPY', '2026-11-12', 10.0)")
    con.commit()
    con.close()
    con = sqlite3.connect(live)                 # live has touches only, one overlapping key
    con.execute(schema)
    con.execute("INSERT INTO touches VALUES ('2026-11-13', 'SPY', 'b', 'desk-b')")
    con.commit()
    con.close()

    out = li.import_zerodte(legacy, live)
    assert out["touches"] == {"legacy": 2, "added": 1}
    assert out["daily_option_volume"] == {"legacy": 1, "added": 1}   # table created from legacy
    assert dict(_rows(live, "SELECT touch_id, payload FROM touches")) == {
        "a": "legacy-a", "b": "desk-b"}
    assert li.import_zerodte(legacy, live)["touches"]["added"] == 0


def test_run_pending_imports_reports_and_retires_the_file(tmp_path):
    ddir = str(tmp_path)
    ldir = os.path.join(ddir, "legacy")
    os.makedirs(ldir)
    assert li.run_pending(ddir) == {}                       # nothing waiting -> no-op
    _agent_db(os.path.join(ldir, "journal.db"), [("2026-11-10", "p", [("SPY", 1)], None)])
    with open(os.path.join(ldir, "opportunities.db"), "w") as f:
        f.write("not a database")                           # one bad file must not block the rest

    ran = li.run_pending(ddir)
    assert ran["agent"]["status"] == "ok" and ran["agent"]["result"]["sessions_added"] == 1
    assert ran["structure"]["status"] == "failed"
    assert os.path.exists(os.path.join(ldir, "journal.db.imported"))
    assert not os.path.exists(os.path.join(ldir, "journal.db"))
    assert os.path.exists(os.path.join(ldir, "opportunities.db"))   # left for another attempt
    assert li.last_report(ddir)["agent"]["status"] == "ok"
    with open(os.path.join(ldir, li.REPORT_NAME)) as f:
        assert set(json.load(f)) == {"agent", "structure"}
    os.remove(os.path.join(ldir, "opportunities.db"))
    assert li.run_pending(ddir) == {}                       # imported file is never re-read

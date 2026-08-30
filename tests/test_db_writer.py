"""Persistence tests with a stubbed Supabase client.

These pin two behaviours that failed silently in the first live run and would
have kept failing indefinitely, because neither raises an error:

  * change detection reads `attributes` from the previous observation. The
    original query selected only fingerprint/signals/observed_at, so
    previous_attributes was always None and write_changes returned 0 on its
    first line, every single run.
  * observations are append-only — the writer must INSERT, never UPDATE.
"""

from typing import Any

import db_writer


class FakeQuery:
    def __init__(self, table: "FakeTable", op: str):
        self.table, self.op, self.selected = table, op, None

    def select(self, columns: str = "*", **kwargs):
        self.selected = columns
        self.table.selects.append(columns)
        return self

    def insert(self, payload):
        self.table.inserted.append(payload)
        return self

    def update(self, payload):
        self.table.updated.append(payload)
        return self

    def upsert(self, payload, **kwargs):
        self.table.upserted.append(payload)
        return self

    def delete(self):
        return self

    def eq(self, *_a, **_k):
        return self

    def neq(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def range(self, *_a, **_k):
        return self

    def execute(self):
        return type("Res", (), {"data": self.table.rows, "count": len(self.table.rows)})()


class FakeTable:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.selects: list[str] = []
        self.inserted: list[Any] = []
        self.updated: list[Any] = []
        self.upserted: list[Any] = []


class FakeSupabase:
    def __init__(self, tables=None):
        self.tables: dict[str, FakeTable] = tables or {}

    def table(self, name: str):
        self.tables.setdefault(name, FakeTable())
        return FakeQuery(self.tables[name], name)


PREV_ATTRS = {
    "email_vendors": ["klaviyo"], "review_vendors": ["judgeme"],
    "sms_vendors": [], "loyalty_vendors": [], "chat_vendors": [],
}


def test_latest_observation_requests_attributes():
    """The bug: without `attributes`, change detection silently never fires."""
    sb = FakeSupabase({"store_observations": FakeTable([{"attributes": PREV_ATTRS}])})
    db_writer.latest_observation(sb, "store-1")
    selected = sb.tables["store_observations"].selects[0]
    assert "attributes" in selected, f"select omits attributes: {selected!r}"


def test_write_changes_detects_install_and_uninstall():
    sb = FakeSupabase()
    current = {**PREV_ATTRS, "review_vendors": [], "loyalty_vendors": ["smile_io"]}
    n = db_writer.write_changes(sb, "store-1", PREV_ATTRS, current)
    assert n == 2
    written = sb.tables["store_changes"].inserted[0]
    events = {(e["change_type"], e["vendor"]) for e in written}
    assert events == {("installed", "smile_io"), ("uninstalled", "judgeme")}


def test_write_changes_silent_when_nothing_moved():
    sb = FakeSupabase()
    assert db_writer.write_changes(sb, "store-1", PREV_ATTRS, dict(PREV_ATTRS)) == 0
    assert "store_changes" not in sb.tables


def test_write_changes_silent_on_first_observation():
    """No previous observation means no diff — not a stream of false installs."""
    sb = FakeSupabase()
    assert db_writer.write_changes(sb, "store-1", None, PREV_ATTRS) == 0


def test_observations_are_inserted_never_updated():
    """History is the product; an update would destroy it."""
    sb = FakeSupabase()
    ok = db_writer.insert_observation(
        sb, "store-1",
        {"base_url": "https://x.example", "script_hosts": [], "class_tokens": [],
         "globals": [], "extension_handles": []},
        {"no_reviews": {"confidence": 0.9, "evidence": {}}},
        {"app_count": 0},
        {"scoreable": True},
    )
    assert ok
    table = sb.tables["store_observations"]
    assert len(table.inserted) == 1
    assert table.updated == [], "observations must never be updated in place"
    assert table.inserted[0]["detector_version"]
    assert table.inserted[0]["fingerprint_hash"]


def test_each_thread_gets_its_own_client():
    """supabase-py wraps a non-thread-safe httpx pool. Sharing one client across
    to_thread workers corrupted it — "deque mutated during iteration" — and lost
    writes while the run still exited 0."""
    import threading

    made: list[object] = []

    def factory():
        client = object()
        made.append(client)
        return client

    db_writer.configure_client_factory(factory)
    try:
        seen: list[int] = []

        def worker():
            seen.append(id(db_writer._client(shared)))
            seen.append(id(db_writer._client(shared)))  # cached within a thread

        shared = object()
        threads = [threading.Thread(target=worker) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(made) == 3, "one client per thread, not per call"
        assert len(set(seen)) == 3, "threads must not share a client"
        assert id(shared) not in seen, "the shared client must never be used"
    finally:
        db_writer._client_factory = None


def test_falls_back_to_shared_client_when_unconfigured():
    """Tests and single-threaded callers never registered a factory."""
    db_writer._client_factory = None
    shared = object()
    assert db_writer._client(shared) is shared


def test_transient_disconnect_is_retried():
    """The sync client's pooled connections are reused across to_thread workers;
    a server-closed socket surfaced as a lost write, not an error the run saw."""
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise Exception("Server disconnected without sending a response.")
        return "ok"

    assert db_writer._retrying("probe", flaky) == "ok"
    assert calls["n"] == 2


def test_real_errors_are_not_retried():
    """Retrying a schema or constraint error just delays the failure."""
    calls = {"n": 0}

    def broken():
        calls["n"] += 1
        raise Exception('column "nope" does not exist')

    try:
        db_writer._retrying("probe", broken)
    except Exception:
        pass
    assert calls["n"] == 1, "non-transient errors must fail on the first attempt"


def test_fetch_status_maps_to_lifecycle_status():
    """`stores.status` means active|dead|not_shopify|unreachable, not the fetch
    outcome. Writing "ok" made every `status = active` filter match nothing."""
    assert db_writer.store_status("ok") == "active"
    assert db_writer.store_status("dead") == "dead"
    assert db_writer.store_status("not_shopify") == "not_shopify"
    assert db_writer.store_status("fetch_failed") == "unreachable"
    assert db_writer.store_status(None) == "unreachable"
    assert db_writer.store_status("something_new") == "unreachable"

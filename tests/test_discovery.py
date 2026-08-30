"""Discovery accounting, verification, and classification boundaries.

These pin three defects that were silent — each produced plausible-looking
output while being wrong, so nothing failed and nobody noticed.
"""

import discovery
import signals


# ── Quota accounting ─────────────────────────────────────────────────────────

class FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"organic_results": []}

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            raise httpx.HTTPStatusError("err", request=None, response=None)

    def json(self):
        return self._payload


class FakeClient:
    """Replays a scripted sequence of responses."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def get(self, url, params=None, timeout=None):
        self.calls += 1
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_request_page_counts_one_search_when_served_first_try(monkeypatch):
    monkeypatch.setattr(discovery.time, "sleep", lambda *_: None)
    client = FakeClient([FakeResponse(200, {"organic_results": [{"link": "x"}]})])
    payload, served = discovery._request_page(client, {})
    assert payload is not None
    assert served == 1


def test_request_page_counts_every_served_retry(monkeypatch):
    """The bug: the ledger incremented once per page while this retried 3x,
    so recorded consumption could be a third of actual."""
    monkeypatch.setattr(discovery.time, "sleep", lambda *_: None)
    client = FakeClient([
        FakeResponse(500),                                    # served, unusable
        FakeResponse(500),                                    # served, unusable
        FakeResponse(200, {"organic_results": [{"link": "x"}]}),  # served, good
    ])
    payload, served = discovery._request_page(client, {})
    assert payload is not None
    assert served == 3, "each served response is a real search"


def test_request_page_does_not_count_rate_limits_or_transport_errors(monkeypatch):
    """A 429 was refused, not served; a transport error never reached them."""
    import httpx
    monkeypatch.setattr(discovery.time, "sleep", lambda *_: None)
    client = FakeClient([
        httpx.ConnectError("no route"),
        FakeResponse(429),
        FakeResponse(200, {"organic_results": []}),
    ])
    _, served = discovery._request_page(client, {})
    assert served == 1


def test_record_extra_usage_books_only_the_surplus(monkeypatch):
    """The pre-flight gate already consumed one; only the remainder is booked."""
    calls = []
    monkeypatch.setattr(discovery, "check_and_increment_quota",
                        lambda sb: calls.append(1))
    discovery.record_extra_usage(object(), 3 - 1)
    assert len(calls) == 2
    calls.clear()
    discovery.record_extra_usage(object(), 1 - 1)
    assert calls == [], "a single served search books nothing extra"


# ── Refresh targeting ────────────────────────────────────────────────────────

class RecordingQuery:
    """Captures the filters a query builder was given."""

    def __init__(self, log):
        self.log = log

    def select(self, *a, **k): return self
    def or_(self, expr): self.log.setdefault("or_", []).append(expr); return self
    def not_(self): return self
    def order(self, *a, **k): return self
    def limit(self, *a, **k): return self

    def in_(self, column, values):
        self.log.setdefault("in_", []).append((column, values))
        return self

    def execute(self):
        return type("Res", (), {"data": []})()


class RecordingSupabase:
    def __init__(self):
        self.log = {}

    def table(self, name):
        q = RecordingQuery(self.log)
        # `.not_` is an attribute that returns a builder exposing `.in_`
        q.not_ = _NotProxy(self.log)
        return q


class _NotProxy:
    def __init__(self, log): self.log = log
    def in_(self, column, values):
        self.log.setdefault("not_in_", []).append((column, values))
        return RecordingQuery(self.log)


def test_refresh_targets_only_sellable_markets():
    """Out-of-market stores are `active`, so without a country filter they were
    re-crawled every 7 days forever and could never become rows."""
    import scraper

    sb = RecordingSupabase()
    scraper._resolve_targets(
        niche=None, country=None, limit=10, supabase=sb, refresh_stale=True,
        freshness_threshold=7, url_arg=None, allow_unmetered=False, urls_file=None,
    )
    assert ("country", ["USA", "Australia"]) in sb.log.get("in_", []), sb.log
    # A LIST, not a string. supabase-py iterates a string character by
    # character, so "(dead,not_shopify)" became NOT IN ('(','d','e','a',...) —
    # true for every row, and the filter excluded nothing at all.
    assert ("status", ["dead", "not_shopify"]) in sb.log.get("not_in_", []), sb.log


# ── Niche classification ─────────────────────────────────────────────────────

def test_classify_niche_ignores_substring_matches():
    """`ring` fired inside spring/watering/offering/catering/measuring, `charm`
    inside charming, `cat` inside catering. A 'Spring Collection' feed scored
    as Jewelry."""
    text = ("spring collection watering can offering catering "
            "measuring engineering charming")
    assert signals.classify_niche({"title": text, "product_titles": []}) is None


def test_classify_niche_still_matches_plurals():
    cases = {
        "Jewelry": ["Gold Rings", "Silver Bracelets", "Diamond Necklaces"],
        "Fashion": ["Summer Dresses", "Denim Jackets", "Cotton Shirts"],
        "Beauty":  ["Vitamin C Serum", "Gentle Cleanser", "Matte Lipsticks"],
        "Pets":    ["Dog Collars", "Cat Toys", "Puppy Treats"],
    }
    for expected, titles in cases.items():
        got = signals.classify_niche({"title": "", "product_titles": titles})
        assert got == expected, f"{titles} -> {got}, expected {expected}"


def test_classify_niche_real_word_still_wins_over_noise():
    got = signals.classify_niche({
        "title": "Spring Sale",
        "product_titles": ["Diamond Ring", "Gemstone Pendant", "Charm Bracelet"],
    })
    assert got == "Jewelry"


# ── Platform-property exclusion ──────────────────────────────────────────────

def test_shopify_own_properties_are_excluded():
    """Shopify's marketing sites live on many ccTLDs and are not storefronts.

    Matching only `shopify.com` let `shopify.com.au` through — and AU discovery
    is scoped `site:*.com.au`, which matches it exactly. It was captured as a
    labelled fixture with five trivially-true gap signals before this was found.
    """
    for host in ("shopify.com", "shopify.com.au", "shopify.co.uk",
                 "help.shopify.com.au", "foo.myshopify.com"):
        assert discovery.is_excluded(f"https://{host}"), host


def test_merchants_merely_mentioning_shopify_are_kept():
    """The label must match exactly — `shopifyexperts.com` is a real business."""
    for host in ("shopifyexperts.com", "realstore.com.au", "mystore.com"):
        assert not discovery.is_excluded(f"https://{host}"), host

"""The Firestore-backed rate limiter (src/rate_limit_firestore.py): one bucket
shared by every instance, same arithmetic as the in-memory one, and a local
fallback when Firestore is down. Firestore itself is replaced by a dict."""

import pytest

import rate_limit_firestore as rlf


class _Snap:
    def __init__(self, data):
        self._data = data
        self.exists = data is not None

    def to_dict(self):
        return dict(self._data)


class _Ref:
    def __init__(self, store, doc_id):
        self.store, self.id = store, doc_id

    def get(self, transaction=None):
        return _Snap(self.store.get(self.id))


class _Txn:
    def set(self, ref, data):
        ref.store[ref.id] = data


class _FakeFirestore:
    """Just what the limiter touches: collection().document(), a transaction."""

    def __init__(self, store):
        self.store = store

    def collection(self, name):
        assert name == rlf.COLLECTION
        return self

    def document(self, doc_id):
        return _Ref(self.store, doc_id)

    def transaction(self):
        return _Txn()


@pytest.fixture(autouse=True)
def _plain_transactional(monkeypatch):
    from google.cloud import firestore

    monkeypatch.setattr(firestore, "transactional", lambda fn: fn)


def _instances(n, store, clock):
    return [rlf.FirestoreRateLimiter(capacity=3, refill_per_sec=1.0,
                                     client=_FakeFirestore(store), clock=clock)
            for _ in range(n)]


def test_instances_share_one_bucket():
    """Three instances, one client: 3 calls in total, not 3 each."""
    store, now = {}, [1000.0]
    a, b, c = _instances(3, store, lambda: now[0])
    results = [lim.check("client-1")[0] for lim in (a, b, c, a, b, c)]
    assert results == [True, True, True, False, False, False]
    now[0] += 1.0  # one second refills one token, for whichever instance asks
    assert c.check("client-1") == (True, 0.0)
    assert a.check("client-1")[0] is False


def test_clients_are_independent_and_ids_dont_store_the_address():
    store = {}
    (lim,) = _instances(1, store, lambda: 0.0)
    for _ in range(3):
        lim.check("203.0.113.7")
    assert lim.check("203.0.113.7")[0] is False
    assert lim.check("198.51.100.9")[0] is True
    assert not any("203.0.113.7" in doc for doc in store)
    assert all("expires_at" in v for v in store.values())


def test_firestore_down_falls_back_to_the_local_bucket():
    class Down(_FakeFirestore):
        def collection(self, name):
            raise RuntimeError("firestore unavailable")

    lim = rlf.FirestoreRateLimiter(capacity=2, refill_per_sec=0.0, client=Down({}))
    assert [lim.check("k")[0] for _ in range(3)] == [True, True, False]


def test_the_api_picks_the_backend_from_settings(monkeypatch):
    from app import agent

    monkeypatch.setattr(agent.settings, "rate_limit_backend", "firestore")
    assert isinstance(agent._make_limiter(), rlf.FirestoreRateLimiter)
    monkeypatch.setattr(agent.settings, "rate_limit_backend", "memory")
    assert type(agent._make_limiter()).__name__ == "RateLimiter"

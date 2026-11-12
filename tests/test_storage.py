from datetime import timedelta

from bugalert.feed import validate_feed
from bugalert.storage import StateStore


def test_baseline_is_not_new_and_discovery_history_survives_restart(
    tmp_path, feed_payload, bug_factory, clock
):
    store = StateStore(tmp_path / "cache" / "state.sqlite3")
    store.initialize()
    initial = store.save(validate_feed(feed_payload), clock(), "seed")
    assert initial.observations[100].baseline
    assert 102 not in initial.observations  # TerminAttr is not an EOS observation.
    first_seen = initial.observations[100].first_seen

    clock.advance(hours=1)
    feed_payload["bugs"].append(bug_factory(999))
    feed_payload["bugs"][0]["alertSummary"] = "Changed summary"
    store.save(validate_feed(feed_payload), clock(), "arista")
    restored = StateStore(store.path).load()
    assert not restored.observations[999].baseline
    assert restored.observations[100].first_seen == first_seen
    assert restored.observations[100].updated_at == clock().isoformat()
    assert restored.fetched_at == clock().isoformat()
    assert restored.source == "arista"


def test_unchanged_content_does_not_move_observation_timestamps(tmp_path, feed_payload, clock):
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    initial = store.save(validate_feed(feed_payload), clock(), "seed")
    clock.advance(hours=2)
    updated = store.save(validate_feed(feed_payload), clock(), "arista")
    assert updated.observations == initial.observations
    assert updated.fetched_at != initial.fetched_at


def test_empty_first_feed_still_establishes_a_baseline(tmp_path, feed_payload, bug_factory, clock):
    store = StateStore(tmp_path / "state.sqlite3")
    store.initialize()
    feed_payload["bugs"] = []
    store.save(validate_feed(feed_payload), clock(), "arista")
    feed_payload["bugs"] = [bug_factory(999)]
    stored = store.save(validate_feed(feed_payload), clock() + timedelta(hours=1), "arista")
    assert not stored.observations[999].baseline

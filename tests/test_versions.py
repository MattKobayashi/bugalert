import pytest

from bugalert.versions import ReleaseGraph, canonical_version, version_sort_key


@pytest.mark.parametrize("version", ["4.31.1", "4.31.1M", "4.31.1F", " 4.31.1m "])
def test_standard_suffixes_are_equivalent(version):
    assert canonical_version(version) == "4.31.1"


def test_special_suffixes_are_not_discarded():
    assert canonical_version("4.31.1FX-Test") == "4.31.1fx-test"
    assert canonical_version("4.10.0-7150") == "4.10.0-7150"


def test_intro_is_inclusive_and_fix_is_exclusive(feed_payload, bug_factory):
    graph = ReleaseGraph(feed_payload)
    bug = bug_factory(versionIntroduced=["4.31.1"], versionFixed=["4.31.2"])
    assert not graph.affected(bug, "4.31.0")
    assert graph.affected(bug, "4.31.1M")
    assert not graph.affected(bug, "4.31.2F")
    assert not graph.affected(bug, "4.31.3")


def test_fix_in_one_train_does_not_clear_a_sibling_train(feed_payload, bug_factory):
    graph = ReleaseGraph(feed_payload)
    bug = bug_factory(versionFixed=["4.30.1"])
    assert not graph.affected(bug, "4.30.2")
    assert graph.affected(bug, "4.31.1")


def test_introduction_does_not_leak_to_a_sibling_train(feed_payload, bug_factory):
    graph = ReleaseGraph(feed_payload)
    bug = bug_factory(versionIntroduced=["4.30.1"], versionFixed=[])
    assert graph.affected(bug, "4.30.2")
    assert not graph.affected(bug, "4.31.1")


def test_reintroduction_after_a_fix_is_affected(feed_payload, bug_factory):
    graph = ReleaseGraph(feed_payload)
    bug = bug_factory(versionIntroduced=["1.0.0", "4.31.2"], versionFixed=["4.31.1", "4.31.3"])
    assert not graph.affected(bug, "4.31.1")
    assert graph.affected(bug, "4.31.2")
    assert not graph.affected(bug, "4.31.3")


def test_nofixyet_is_not_a_fix(feed_payload, bug_factory):
    graph = ReleaseGraph(feed_payload)
    bug = bug_factory(versionFixed=["4.31.nofixyet"])
    assert graph.affected(bug, "4.31.3")
    assert graph.next_fix(bug, "4.31.1") is None


def test_unknown_release_is_not_guessed_numerically(feed_payload, bug_factory):
    graph = ReleaseGraph(feed_payload)
    assert not graph.knows("4.31.99M")
    assert not graph.affected(bug_factory(), "4.31.99M")


def test_non_eos_alerts_are_excluded(feed_payload, bug_factory):
    assert not ReleaseGraph(feed_payload).affected(bug_factory(product="mwm"), "4.31.1")


def test_special_branch_does_not_get_cleared_by_a_mainline_fix(feed_payload, bug_factory):
    graph = ReleaseGraph(feed_payload)
    assert graph.affected(bug_factory(versionFixed=["4.31.2"]), "4.31.1FX-Test.2")


def test_same_train_fix_must_be_reachable_and_not_a_special_build(feed_payload, bug_factory):
    graph = ReleaseGraph(feed_payload)
    bug = bug_factory(versionFixed=["4.30.1", "4.31.1FX-Test.2", "4.31.2", "4.32.2"])
    assert graph.next_fix(bug, "4.31.1M") == "4.31.2"
    assert graph.next_fix(bug, "4.31.1FX-Test") == "4.31.1FX-Test.2"


def test_graph_cycles_do_not_hang(feed_payload, bug_factory):
    feed_payload["releaseGraph"].append(["4.31.3", "4.31.0"])
    graph = ReleaseGraph(feed_payload)
    assert graph.knows("4.31.2")
    assert graph.affected(bug_factory(versionFixed=[]), "4.31.2")


def test_versions_sort_numerically():
    assert sorted(["4.31.10", "4.31.2", "4.31.2.1"], key=version_sort_key) == [
        "4.31.2",
        "4.31.2.1",
        "4.31.10",
    ]


def test_declared_but_disconnected_releases_are_not_evaluated(feed_payload, bug_factory):
    feed_payload["releases"].append({"release": "4.99.1FX-Test", "product": "eos"})
    feed_payload["releaseGraph"].append(["4.99.1FX-Test", "4.99.1FX-Test.2"])
    graph = ReleaseGraph(feed_payload)
    assert not graph.knows("4.99.1FX-Test")
    assert not graph.knows("4.99.1FX-Test.2")
    assert not graph.affected(bug_factory(versionFixed=[]), "4.99.1FX-Test")

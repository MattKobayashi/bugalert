"""EOS release-graph matching, including branch-local fixes and reintroductions."""

import re
from collections import defaultdict
from typing import Any


def canonical_version(version: str) -> str:
    # Arista's graph omits the standard feature/maintenance designation. Never
    # strip FX or hardware-specific suffixes: those are genuinely separate nodes.
    version = version.strip()
    match = re.fullmatch(r"(\d+(?:\.\d+){2,})[FM]", version, flags=re.IGNORECASE)
    return (match.group(1) if match else version).casefold()


def version_sort_key(version: str) -> tuple:
    match = re.match(r"\d+(?:\.\d+)*", canonical_version(version))
    parts = tuple(map(int, match.group().split("."))) if match else ()
    return (*parts, *(0 for _ in range(max(0, 8 - len(parts))))), canonical_version(version)


class ReleaseGraph:
    def __init__(self, payload: dict[str, Any]):
        self.parents: dict[str, set[str]] = defaultdict(set)
        self.children: dict[str, set[str]] = defaultdict(set)
        self.nodes: set[str] = set()
        self._ancestor_cache: dict[str, frozenset[str]] = {}
        self._descendant_cache: dict[str, frozenset[str]] = {}
        for parent, child in payload["releaseGraph"]:
            parent, child = canonical_version(parent), canonical_version(child)
            self.parents[child].add(parent)
            self.children[parent].add(child)
            self.nodes.update((parent, child))
        for release in payload.get("releases", []):
            if release.get("product", "").casefold() == "eos":
                self.nodes.add(canonical_version(release["release"]))

    def ancestors(self, version: str) -> frozenset[str]:
        version = canonical_version(version)
        if version not in self._ancestor_cache:
            self._ancestor_cache[version] = self._walk(version, self.parents)
        return self._ancestor_cache[version]

    def descendants(self, version: str) -> frozenset[str]:
        version = canonical_version(version)
        if version not in self._descendant_cache:
            self._descendant_cache[version] = self._walk(version, self.children)
        return self._descendant_cache[version]

    @staticmethod
    def _walk(start: str, edges: dict[str, set[str]]) -> frozenset[str]:
        visited, pending = set(), [start]
        while pending:
            node = pending.pop()
            if node not in visited:
                visited.add(node)
                pending.extend(edges.get(node, ()))
        return frozenset(visited)

    def knows(self, version: str) -> bool:
        # A release can be declared in `releases` but have a disconnected or
        # missing lineage. Such releases cannot safely be reported as clear.
        return canonical_version(version) in self.nodes and "1.0.0" in self.ancestors(version)

    def affected(self, bug: dict, version: str) -> bool:
        if bug["product"].casefold() != "eos" or not self.knows(version):
            return False
        introduced = {canonical_version(value) for value in bug["versionIntroduced"]}
        fixed = {
            canonical_version(value)
            for value in bug["versionFixed"]
            if "nofixyet" not in value.casefold()
        }
        lineage = self.ancestors(version)
        if not introduced.intersection(lineage):
            return False
        if not fixed.intersection(lineage):
            return True

        # Walk backwards, stopping at each fix. An earlier fix in another train
        # must not clear this train, and a later introduction can reopen a bug.
        visited, pending = set(), [canonical_version(version)]
        while pending:
            node = pending.pop()
            if node in visited or node in fixed:
                continue
            visited.add(node)
            if node in introduced:
                return True
            pending.extend(self.parents.get(node, ()))
        return False

    def next_fix(self, bug: dict, version: str) -> str | None:
        """Earliest listed, reachable fix in the same train; not an upgrade guarantee."""
        canonical = canonical_version(version)
        train = canonical.split(".")[:2]
        mainstream = bool(re.fullmatch(r"\d+(?:\.\d+)*", canonical))
        descendants = self.descendants(version)
        candidates = []
        for fixed in bug["versionFixed"]:
            normalized = canonical_version(fixed)
            if "nofixyet" in normalized or normalized.split(".")[:2] != train:
                continue
            if mainstream and not re.fullmatch(r"\d+(?:\.\d+)*", normalized):
                continue
            if normalized in descendants:
                candidates.append(fixed)
        return min(candidates, key=version_sort_key) if candidates else None

"""A browser-safe, version-filtered view of the feed."""

import json
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit

from bugalert.config import Settings
from bugalert.storage import StoredFeed
from bugalert.versions import ReleaseGraph


def _exported_at(value: object) -> str | None:
    if type(value) not in (int, float) or value <= 0:
        return None
    try:
        return datetime.fromtimestamp(value, UTC).isoformat()
    except ValueError, OverflowError, OSError:
        return None


def advisory_url(value: str) -> str | None:
    value = re.sub(r"^https:\s*//", "https://", value.strip(), flags=re.IGNORECASE)
    try:
        url = urlsplit(value)
        if (
            url.scheme == "https"
            and url.hostname in {"www.arista.com", "arista.com"}
            and not url.username
            and not url.password
            and url.port in (None, 443)
        ):
            return value
    except ValueError:
        pass
    return None


class Catalog:
    def __init__(self, stored: StoredFeed | None, settings: Settings, now: datetime):
        self.alerts: list[dict] = []
        self.releases: list[dict] = []
        self.details: dict[int, dict] = {}
        self.total_eos = 0
        self.next_expiry: datetime | None = None
        graph = ReleaseGraph(stored.feed.payload) if stored else None
        counts = {version: 0 for version in settings.versions}
        security_counts = dict.fromkeys(settings.versions, 0)
        cutoff = now - timedelta(hours=settings.new_window_hours)

        for bug in stored.feed.bugs if stored else []:
            if bug["product"].casefold() != "eos":
                continue
            self.total_eos += 1
            affected = [version for version in settings.versions if graph.affected(bug, version)]
            observation = stored.observations.get(bug["bugId"])
            is_new = bool(
                observation
                and not observation.baseline
                and datetime.fromisoformat(observation.first_seen) > cutoff
            )
            cves = sorted(set(re.findall(r"CVE-\d{4}-\d{4,}", bug.get("cve", "").upper())))
            security = bool(
                bug.get("cve", "").strip() or bug.get("securityAdvisoryURL", "").strip()
            )
            summary = {
                "id": bug["bugId"],
                "summary": " ".join(bug["alertSummary"].split()),
                "severity": bug["severity"].casefold(),
                "is_security": security,
                "cves": cves,
                "affected_versions": affected,
                "fixes_by_version": {version: graph.next_fix(bug, version) for version in affected},
                "exported_at": _exported_at(bug.get("exportedTime")),
                "is_new": is_new,
                "first_seen_at": observation.first_seen if observation else None,
                "updated_at": observation.updated_at if observation else None,
            }
            if affected:
                self.alerts.append(summary)
                if is_new:
                    expiry = datetime.fromisoformat(observation.first_seen) + timedelta(
                        hours=settings.new_window_hours
                    )
                    self.next_expiry = min(self.next_expiry, expiry) if self.next_expiry else expiry
                for version in affected:
                    counts[version] += 1
                    security_counts[version] += int(security)

            conditions = []
            conjunction = bug.get("conjunction", [])
            if isinstance(conjunction, list):
                for group in conjunction:
                    if isinstance(group, list):
                        conditions.append(
                            [
                                condition["tag"]
                                if isinstance(condition, dict) and set(condition) == {"tag"}
                                else json.dumps(condition, ensure_ascii=False, sort_keys=True)
                                for condition in group
                            ]
                        )
            self.details[bug["bugId"]] = {
                **summary,
                "note": bug.get("alertNote", ""),
                "release_note": bug.get("releaseNote", ""),
                "introduced_versions": bug["versionIntroduced"],
                "fixed_versions": bug["versionFixed"],
                "advisory_url": advisory_url(bug.get("securityAdvisoryURL", "")),
                "conditions": conditions,
            }

        self.alerts.sort(key=lambda alert: (alert["exported_at"] or "", alert["id"]), reverse=True)
        for version in settings.versions:
            known = bool(graph and graph.knows(version))
            self.releases.append(
                {
                    "version": version,
                    "known": known,
                    "alert_count": counts[version] if known else None,
                    "security_count": security_counts[version] if known else None,
                }
            )
        self.summary = {
            "total": len(self.alerts),
            "bugs": sum(not alert["is_security"] for alert in self.alerts),
            "security": sum(alert["is_security"] for alert in self.alerts),
            "severity_one": sum(alert["severity"] == "sev1" for alert in self.alerts),
            "new": sum(alert["is_new"] for alert in self.alerts),
        }

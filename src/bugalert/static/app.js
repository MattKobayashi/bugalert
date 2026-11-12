(() => {
  "use strict";

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const PAGE_SIZE = 25;
  const state = {
    data: null,
    kind: "all",
    onlyNew: false,
    severity: "all",
    release: "all",
    query: "",
    sort: "newest",
    page: 0,
    connected: false,
    manualRefresh: false,
    detailId: null,
  };
  let fetching = false;
  let fetchQueued = false;
  let toastTimer;
  let detailController;
  let initialAlert = new URL(location.href).searchParams.get("alert");

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
  const icon = (name) => `<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
  const number = (value) => new Intl.NumberFormat("en").format(value ?? 0);
  const date = (value, withTime = false) => {
    if (!value) return "Not provided";
    const parsed = new Date(value);
    if (Number.isNaN(parsed.getTime())) return "Not provided";
    return new Intl.DateTimeFormat("en-GB", {
      day: "2-digit", month: "short", year: "numeric", timeZone: "UTC",
      ...(withTime ? { hour: "2-digit", minute: "2-digit", timeZoneName: "short" } : {}),
    }).format(parsed);
  };
  const relativeTime = (value) => {
    if (!value) return "Not checked yet";
    const seconds = Math.max(0, (Date.now() - new Date(value).getTime()) / 1000);
    if (seconds < 60) return "Just now";
    if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} hr ago`;
    return date(value);
  };
  const severityLabel = (severity) => ({ sev1: "Sev 1", sev2: "Sev 2", sev3: "Sev 3" })[severity] || "Other";
  const severityBadge = (severity) => {
    const level = ["sev1", "sev2", "sev3"].includes(severity) ? severity : "other";
    return `<span class="severity-badge ${level}" title="Arista feed severity; not a CVSS score">${severityLabel(severity)}</span>`;
  };

  function showToast(message) {
    clearTimeout(toastTimer);
    $("#toast-message").textContent = message;
    $("#toast").hidden = false;
    toastTimer = setTimeout(() => { $("#toast").hidden = true; }, 6500);
  }

  async function copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      showToast("Copied to clipboard.");
    } catch {
      showToast("Clipboard access is unavailable. Select and copy the text instead.");
    }
  }

  function filteredAlerts() {
    if (!state.data) return [];
    const terms = state.query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    const alerts = state.data.alerts.filter((alert) => {
      if (state.kind === "security" && !alert.is_security) return false;
      if (state.kind === "bug" && alert.is_security) return false;
      if (state.onlyNew && !alert.is_new) return false;
      if (state.severity !== "all" && alert.severity !== state.severity) return false;
      if (state.release !== "all" && !alert.affected_versions.includes(state.release)) return false;
      if (terms.length) {
        const haystack = [alert.id, `bug-${alert.id}`, alert.summary, ...alert.cves,
          ...alert.affected_versions, alert.severity].join(" ").toLowerCase();
        if (!terms.every((term) => haystack.includes(term))) return false;
      }
      return true;
    });
    const rank = { sev1: 1, sev2: 2, sev3: 3 };
    return alerts.sort((a, b) => {
      if (state.sort === "severity") {
        const comparison = (rank[a.severity] || 4) - (rank[b.severity] || 4);
        if (comparison) return comparison;
      }
      if (state.sort === "discovered") {
        const comparison = (b.first_seen_at || "").localeCompare(a.first_seen_at || "");
        if (comparison) return comparison;
      }
      return (b.exported_at || "").localeCompare(a.exported_at || "") || b.id - a.id;
    });
  }

  function resetFilters() {
    Object.assign(state, { kind: "all", onlyNew: false, severity: "all", release: "all",
      query: "", sort: "newest", page: 0 });
    syncFilterControls();
  }

  function syncFilterControls() {
    $("#search-input").value = state.query;
    $("#severity-filter").value = state.severity;
    $("#release-filter").value = state.release;
    $("#sort-select").value = state.sort;
  }

  function renderNavigation() {
    $$("[data-nav]").forEach((button) => {
      const selected = button.dataset.nav === "new" ? state.onlyNew
        : !state.onlyNew && (button.dataset.nav === "security" ? state.kind === "security" : state.kind !== "security");
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    $$("[data-kind]").forEach((button) => {
      const selected = button.dataset.kind === state.kind;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
    const allReleases = state.release === "all";
    $("#all-releases").classList.toggle("is-active", allReleases);
    $("#all-releases").setAttribute("aria-pressed", String(allReleases));
    $$("[data-release]").forEach((button) => {
      const selected = button.dataset.release === state.release;
      button.classList.toggle("is-active", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
  }

  function renderOverview() {
    const { summary, releases, config } = state.data;
    $("#stat-total").textContent = number(summary.total);
    $("#stat-security").textContent = number(summary.security);
    $("#stat-severity").textContent = number(summary.severity_one);
    $("#stat-new").textContent = number(summary.new);
    $("#nav-total").textContent = number(summary.total);
    $("#nav-security").textContent = number(summary.security);
    $("#nav-new").textContent = number(summary.new);
    $("#new-window-label").innerHTML = `First seen in the last ${number(config.new_window_hours)} hours${icon("arrow")}`;
    $("#release-list").innerHTML = releases.map((release) => `
      <button class="release-item" data-release="${esc(release.version)}" aria-pressed="false">
        <span class="release-led${release.known ? "" : " is-unknown"}"></span>
        <span><span class="release-version">${esc(release.version)}</span>
          <span class="release-caption">${release.known ? `${number(release.security_count)} security alerts` : "No lineage · not evaluated"}</span></span>
        <span class="release-total">${release.known ? number(release.alert_count) : "?"}</span>
      </button>`).join("");
    $("#release-filter").innerHTML = '<option value="all">All releases</option>' + releases.map(
      (release) => `<option value="${esc(release.version)}">${esc(release.version)}${release.known ? "" : " (unknown)"}</option>`
    ).join("");
    if (!releases.some((release) => release.version === state.release)) state.release = "all";
    syncFilterControls();
    renderConfiguration();
    renderBanners();
    updateFeedStatus();
  }

  function renderBanners() {
    const { feed, config, warnings, releases } = state.data;
    const notices = [];
    if (feed.source === "seed") {
      notices.push(`<div class="notice">${icon("info")}<div class="notice-content"><strong>Local snapshot, not a live feed.</strong>
        <p>Published ${esc(date(feed.release_date))}. ${feed.token_configured
          ? "The monitor will replace this snapshot after a successful Arista feed check."
          : `Set <code>${esc(config.token_env)}</code> on the server and restart to enable scheduled updates.`}</p></div>
        <button class="text-button" data-open-config>Setup details ↗</button></div>`);
    } else if (feed.error_code === "missing_token") {
      notices.push(`<div class="notice">${icon("info")}<div class="notice-content"><strong>Live checks are paused.</strong>
        <p>${esc(feed.error)} Your last downloaded feed is still available.</p></div><button class="text-button" data-open-config>Setup details ↗</button></div>`);
    }
    if (feed.error && feed.error_code !== "missing_token") {
      notices.push(`<div class="notice is-error">${icon("alert")}<div class="notice-content"><strong>The last feed check did not succeed.</strong><p>${esc(feed.error)}${feed.last_success_at ? ` Last successful check: ${esc(date(feed.last_success_at, true))}.` : ""}</p></div></div>`);
    }
    if (config.error) {
      notices.push(`<div class="notice is-error">${icon("alert")}<div class="notice-content"><strong>Configuration needs attention. Last valid settings are still active.</strong><p>${esc(config.error)}</p></div></div>`);
    }
    const unknown = releases.filter((release) => !release.known);
    if (unknown.length) {
      notices.push(`<div class="notice">${icon("alert")}<div class="notice-content"><strong>${number(unknown.length)} ${unknown.length === 1 ? "release could" : "releases could"} not be evaluated.</strong>
        <p>${unknown.map((release) => `<code>${esc(release.version)}</code>`).join(", ")} ${unknown.length === 1 ? "has" : "have"} no complete lineage in the current release graph. These releases are not included in alert counts; a zero count does not mean they are unaffected.</p></div></div>`);
    }
    warnings.forEach((warning) => notices.push(`<div class="notice">${icon("info")}<div class="notice-content"><strong>Feed loading note</strong><p>${esc(warning)}</p></div></div>`));
    $("#banner-stack").innerHTML = notices.join("");
  }

  function updateFeedStatus() {
    if (!state.data) return;
    const { feed } = state.data;
    const noToken = !feed.token_configured;
    const refreshing = feed.refreshing || state.manualRefresh;
    const sample = feed.source === "seed";
    const needsAttention = Boolean(feed.error && feed.error_code !== "missing_token");
    const title = !state.connected ? "Reconnecting to app" : refreshing ? "Checking feed"
      : needsAttention ? "Feed needs attention" : sample ? "Snapshot mode"
        : noToken ? "Live checks paused" : feed.source ? "Live monitoring" : "Awaiting first feed";
    const warning = !state.connected || needsAttention || sample || noToken;
    $("#connection-text").textContent = title;
    $("#connection-pill").classList.toggle("is-warning", warning);
    $("#health-dot").classList.toggle("is-warning", warning);
    $("#health-title").textContent = title;
    $("#health-subtitle").textContent = sample ? `Snapshot · ${date(feed.release_date)}`
      : feed.last_success_at ? `Last success · ${relativeTime(feed.last_success_at)}` : "No successful download yet";
    $("#last-check").textContent = feed.error_code === "missing_token" ? "Paused · no token"
      : relativeTime(feed.last_attempt_at);
    $("#last-check").title = feed.last_attempt_at ? date(feed.last_attempt_at, true) : "No upstream feed check yet";
    const remaining = Math.max(0, Math.ceil((new Date(feed.next_refresh_at).getTime() - Date.now()) / 1000));
    $("#next-check").textContent = refreshing ? "Contacting Arista securely…" : noToken
      ? "Live updates need a server token" : remaining > 0
        ? `Next check in ${Math.floor(remaining / 60)}m ${String(remaining % 60).padStart(2, "0")}s` : "Next check shortly";
    $("#refresh-button").disabled = refreshing;
    $("#refresh-button").classList.toggle("is-spinning", refreshing);
    $("#refresh-button").setAttribute("aria-busy", String(refreshing));
    $("#refresh-button span").textContent = refreshing ? "Checking for updates…" : "Check for updates";
    $("#source-stamp").textContent = feed.release_date ? `FEED PUBLISHED ${date(feed.release_date).toUpperCase()} · ${number(feed.total_eos_alerts)} EOS ALERTS` : "NO FEED LOADED";
    $("#generation-label").textContent = feed.generation_id ? `GEN / ${feed.generation_id.slice(0, 8).toUpperCase()}` : "EOS / BUGALERT";
  }

  function fixCell(alert) {
    const fixes = Object.values(alert.fixes_by_version);
    const listed = [...new Set(fixes.filter(Boolean))];
    if (!listed.length) return '<span class="fix-status is-missing">No fix listed</span><span class="fix-version">In monitored trains</span>';
    const partial = fixes.some((fix) => !fix);
    return `<span class="fix-status${partial ? " is-partial" : ""}">${partial ? "Partial fix coverage" : "Fix listed"}</span><span class="fix-version">${esc(listed[0])}${listed.length > 1 ? ` +${listed.length - 1}` : ""}</span>`;
  }

  function alertRow(alert) {
    const versions = alert.affected_versions;
    return `<tr class="alert-row${alert.is_new ? " is-new" : ""}" data-alert="${alert.id}">
      <td><div class="alert-eyebrow"><span class="alert-kind${alert.is_security ? " is-security" : ""}">${icon(alert.is_security ? "shield" : "bug")}${alert.is_security ? "SECURITY" : "BUG"}</span><span class="alert-id">${alert.id}</span>${alert.is_new ? '<span class="new-badge">NEW</span>' : ""}</div>
        <button class="alert-title" data-alert="${alert.id}" title="${esc(alert.summary)}">${esc(alert.summary || `Bug ${alert.id}`)}</button>
        ${alert.cves.length ? `<div class="cve-list">${alert.cves.slice(0, 2).map((cve) => `<span class="cve-chip">${esc(cve)}</span>`).join("")}${alert.cves.length > 2 ? `<span class="cve-chip">+${alert.cves.length - 2}</span>` : ""}</div>` : ""}</td>
      <td>${severityBadge(alert.severity)}</td>
      <td><div class="version-chips" title="${esc(versions.join(", "))}">${versions.slice(0, 2).map((version) => `<span class="version-chip">${esc(version)}</span>`).join("")}${versions.length > 2 ? `<span class="version-chip">+${versions.length - 2}</span>` : ""}</div></td>
      <td>${fixCell(alert)}</td><td><time class="exported-date"${alert.exported_at ? ` datetime="${esc(alert.exported_at)}"` : ""}>${esc(alert.exported_at ? date(alert.exported_at) : "—")}</time></td>
    </tr>`;
  }

  function renderAlerts() {
    if (!state.data) return;
    const alerts = filteredAlerts();
    const pages = Math.max(1, Math.ceil(alerts.length / PAGE_SIZE));
    state.page = Math.min(state.page, pages - 1);
    const start = state.page * PAGE_SIZE;
    const pageAlerts = alerts.slice(start, start + PAGE_SIZE);
    const title = state.onlyNew ? "New discoveries" : state.kind === "security"
      ? "Security alerts" : state.kind === "bug" ? "Bug alerts" : "All alerts";
    $("#register-title").textContent = title;
    $("#result-count").textContent = number(alerts.length);
    const releaseDescription = state.release !== "all" ? state.release
      : `${state.data.releases.filter((release) => release.known).length} evaluated releases`;
    const unevaluated = state.release !== "all"
      ? state.data.releases.find((release) => release.version === state.release && !release.known)
      : state.data.releases.every((release) => !release.known);
    $("#register-description").textContent = unevaluated
      ? "No complete release lineage is available for this view. Exposure has not been evaluated."
      : state.onlyNew
      ? `First seen after the baseline import, within ${state.data.config.new_window_hours} hours.`
      : `Potentially affecting ${releaseDescription}. Select an alert for the full picture.`;
    $("#alerts-body").innerHTML = pageAlerts.length ? pageAlerts.map(alertRow).join("")
      : `<tr><td colspan="5"><div class="empty-state">${icon("search")}<h3>${state.data.releases.every((release) => !release.known) ? "These releases have not been evaluated" : "No alerts in this view"}</h3><p>${state.data.releases.every((release) => !release.known)
        ? "Check your TOML versions and feed status. Unknown releases cannot be treated as unaffected."
        : state.onlyNew ? "No newly discovered matching alerts in the current window. Existing alerts are still in All alerts."
          : "Try another search, severity, or monitored release. This is a filtered feed view, not a guarantee of no exposure."}</p><button class="button button-secondary" data-reset-filters>Show all matching alerts</button></div></td></tr>`;
    $("#table-scroll").setAttribute("aria-busy", "false");
    $("#visible-count").textContent = alerts.length ? `Showing ${number(start + 1)}–${number(Math.min(start + PAGE_SIZE, alerts.length))} of ${number(alerts.length)} alerts` : "No matching alerts to display";
    $("#page-indicator").textContent = `${String(state.page + 1).padStart(2, "0")} / ${String(pages).padStart(2, "0")}`;
    $("#previous-page").disabled = state.page === 0;
    $("#next-page").disabled = state.page + 1 >= pages;
    $("#export-button").disabled = alerts.length === 0;
    const filters = [];
    if (state.onlyNew) filters.push("New discoveries");
    if (state.kind !== "all") filters.push(state.kind === "security" ? "Security only" : "Bugs only");
    if (state.severity !== "all") filters.push(severityLabel(state.severity));
    if (state.release !== "all") filters.push(state.release);
    if (state.query) filters.push(`“${state.query}”`);
    $("#active-filters").hidden = filters.length === 0;
    $("#filter-summary").textContent = filters.join(" · ");
    renderNavigation();
  }

  function renderConfiguration() {
    const { config, feed } = state.data;
    $("#config-path").textContent = config.path;
    $("#config-code").textContent = `[eos]\nversions = [${config.versions.map((version) => JSON.stringify(version)).join(", ")}]\n\n[feed]\nrefresh_seconds = ${config.refresh_seconds}\nnew_window_hours = ${config.new_window_hours}\ntoken_env = "${config.token_env}"`;
    $("#token-command").textContent = `export ${config.token_env}='your-token'`;
    $("#config-token-status").textContent = feed.token_configured
      ? "A token is configured on the server. It is never sent to this browser."
      : "No token is configured. Set this environment variable before starting the server. Restart an already running server after setting it.";
  }

  async function loadDashboard() {
    if (fetching) { fetchQueued = true; return; }
    fetching = true;
    do {
      fetchQueued = false;
      try {
        const response = await fetch("/api/dashboard", { cache: "no-store" });
        if (!response.ok) throw new Error("Dashboard request failed");
        const data = await response.json();
        const previous = state.data;
        state.data = data;
        if (!previous || previous.revision !== data.revision) {
          const previousIds = new Set(previous?.alerts.map((alert) => alert.id) || []);
          const added = previous ? data.alerts.filter((alert) => alert.is_new && !previousIds.has(alert.id)).length : 0;
          renderOverview();
          renderAlerts();
          if (added) showToast(`${number(added)} newly discovered ${added === 1 ? "alert now matches" : "alerts now match"} your releases. Find ${added === 1 ? "it" : "them"} in New discoveries.`);
          if (state.detailId && $("#detail-dialog").open) void loadDetail(state.detailId);
        } else updateFeedStatus();
        if (initialAlert && /^\d+$/.test(initialAlert)) {
          const id = Number(initialAlert);
          initialAlert = null;
          if (Number.isSafeInteger(id) && id > 0) void openDetail(id);
        }
      } catch {
        if (!state.data) {
          $("#alerts-body").innerHTML = `<tr><td colspan="5"><div class="empty-state">${icon("alert")}<h3>Cannot reach the monitor</h3><p>The browser will reconnect automatically. Check that the Bugalert server is running.</p><button class="button button-secondary" id="retry-dashboard">Try again</button></div></td></tr>`;
          $("#table-scroll").setAttribute("aria-busy", "false");
          $("#visible-count").textContent = "The feed has not loaded.";
        }
        state.connected = false;
        updateFeedStatus();
      }
    } while (fetchQueued);
    fetching = false;
  }

  function detailMarkup(alert) {
    const boundaries = (versions) => versions.length ? versions.map((version) => `<span class="version-chip${version.toLowerCase().includes("nofixyet") ? " is-unfixed" : ""}">${esc(version)}</span>`).join("") : '<span class="detail-note">None listed in the feed.</span>';
    const impacts = alert.affected_versions.length ? alert.affected_versions.map((version) => {
      const fix = alert.fixes_by_version[version];
      return `<div class="impact-row"><div><span class="impact-version">${esc(version)}</span><small>Potentially affected</small></div><div class="impact-fix${fix ? "" : " is-missing"}">${fix ? `Same-train fix listed<span class="mono">${esc(fix)}</span>` : "No same-train fix listed"}</div></div>`;
    }).join("") : '<p class="detail-note">This alert does not currently match an evaluated release in your watchlist.</p>';
    const conditions = alert.conditions.length ? alert.conditions.map((group) => `<div class="condition-group">${group.map((condition) => `<span class="condition-tag">${esc(condition)}</span>`).join('<span class="condition-operator">AND</span>')}</div>`).join('<div class="condition-operator condition-or">OR</div>')
      : '<p class="detail-note">No hardware or enabled-feature conditions are included in this alert.</p>';
    const note = alert.note || alert.release_note || alert.summary;
    return `<div class="detail-meta-row"><span class="alert-kind${alert.is_security ? " is-security" : ""}">${icon(alert.is_security ? "shield" : "bug")}${alert.is_security ? "SECURITY ALERT" : "BUG ALERT"}</span>${severityBadge(alert.severity)}${alert.is_new ? '<span class="new-badge">NEW DISCOVERY</span>' : ""}</div>
      <h2 id="detail-title">${esc(alert.summary)}</h2>
      ${alert.cves.length ? `<div class="detail-cves">${alert.cves.map((cve) => `<a class="cve-chip" href="https://www.cve.org/CVERecord?id=${encodeURIComponent(cve)}" target="_blank" rel="noopener noreferrer">${esc(cve)} ↗</a>`).join("")}</div>` : ""}
      ${alert.advisory_url ? `<a class="button button-secondary advisory-link" href="${esc(alert.advisory_url)}" target="_blank" rel="noopener noreferrer">Read Arista security advisory ${icon("external")}</a>` : ""}
      <section class="detail-section"><h3>Monitored release exposure</h3><div class="impact-list">${impacts}</div><div class="detail-disclaimer">${icon("info")}<span>Version-based screening only. Hardware, enabled features, and mitigations can change actual applicability. Listed fixes are not a tested upgrade recommendation.</span></div></section>
      <section class="detail-section"><h3>What to know</h3><p class="detail-note">${esc(note)}</p></section>
      ${alert.release_note && alert.release_note.trim() !== note.trim() ? `<section class="detail-section"><h3>Release note</h3><p class="detail-note">${esc(alert.release_note)}</p></section>` : ""}
      <section class="detail-section"><h3>Release boundaries from the feed</h3><p class="boundary-label">INTRODUCED IN</p><div class="boundary-chips">${boundaries(alert.introduced_versions)}</div><p class="boundary-label">FIXED IN / NO-FIX-YET MARKERS</p><div class="boundary-chips">${boundaries(alert.fixed_versions)}</div></section>
      <section class="detail-section"><h3>Hardware &amp; feature conditions</h3>${conditions}</section>
      <section class="detail-section"><h3>Feed metadata</h3><dl class="detail-metadata"><div><dt>Bug ID</dt><dd>${alert.id}</dd></div><div><dt>Exported by Arista</dt><dd>${esc(date(alert.exported_at))}</dd></div><div><dt>First seen locally</dt><dd>${esc(date(alert.first_seen_at))}</dd></div><div><dt>Last content change observed</dt><dd>${esc(date(alert.updated_at))}</dd></div></dl></section>`;
  }

  async function loadDetail(id) {
    detailController?.abort();
    detailController = new AbortController();
    try {
      const response = await fetch(`/api/alerts/${id}`, { signal: detailController.signal, cache: "no-store" });
      if (!response.ok) throw new Error("Alert unavailable");
      const alert = await response.json();
      if (state.detailId !== id) return;
      $("#detail-body").innerHTML = detailMarkup(alert);
      $("#detail-kicker").textContent = `EOS / ALERT ${alert.id}`;
    } catch (error) {
      if (error.name === "AbortError" || state.detailId !== id) return;
      $("#detail-body").innerHTML = '<h2 id="detail-title">Alert unavailable</h2><p class="detail-note">This alert may no longer be in the current feed, or the server is temporarily unavailable. Close this panel and try again.</p>';
    }
  }

  async function openDetail(id) {
    setSidebar(false);
    if ($("#config-dialog").open) $("#config-dialog").close();
    state.detailId = id;
    $("#detail-kicker").textContent = `EOS / ALERT ${id}`;
    $("#detail-body").innerHTML = '<h2 id="detail-title">Loading alert…</h2><div class="loading-state"><span class="loading-orbit" aria-hidden="true"></span></div>';
    if (!$("#detail-dialog").open) $("#detail-dialog").showModal();
    $("#detail-dialog").scrollTop = 0;
    const url = new URL(location.href);
    url.searchParams.set("alert", String(id));
    history.replaceState(null, "", url);
    await loadDetail(id);
  }

  function openConfiguration() {
    setSidebar(false);
    if ($("#detail-dialog").open) $("#detail-dialog").close();
    if (!$("#config-dialog").open) $("#config-dialog").showModal();
  }

  async function refreshFeed() {
    if (state.manualRefresh || state.data?.feed.refreshing) return;
    state.manualRefresh = true;
    updateFeedStatus();
    try {
      const response = await fetch("/api/refresh", {
        method: "POST", headers: { "X-Bugalert-Request": "refresh", "Content-Type": "application/json" }, body: "{}",
      });
      if (!response.ok) throw new Error("Refresh request failed");
      const result = await response.json();
      showToast(result.ok ? "Feed checked. Your release view is up to date."
        : result.refreshing ? "A feed check is already in progress."
          : "Feed check did not complete. See the feed status for details.");
    } catch {
      showToast("Cannot reach the monitor. The last displayed feed has been kept.");
    } finally {
      state.manualRefresh = false;
      await loadDashboard();
      updateFeedStatus();
    }
  }

  function exportCSV() {
    const alerts = filteredAlerts();
    if (!alerts.length) return;
    const cell = (value) => {
      let text = String(value ?? "");
      // Prevent spreadsheet formulas in untrusted upstream summaries.
      if (/^[\s\u0000-\u001f]*[=+\-@]/.test(text)) text = `'${text}`;
      return `"${text.replace(/"/g, '""')}"`;
    };
    const rows = [["Bug ID", "Type", "Severity", "Summary", "CVEs", "Affected monitored releases",
      "Same-train fixes (per release)", "Exported UTC", "New discovery", "First seen UTC"]];
    alerts.forEach((alert) => rows.push([alert.id, alert.is_security ? "Security" : "Bug", alert.severity,
      alert.summary, alert.cves.join("; "), alert.affected_versions.join("; "),
      Object.entries(alert.fixes_by_version).map(([version, fix]) => `${version}: ${fix || "none listed"}`).join("; "),
      alert.exported_at, alert.is_new ? "Yes" : "No", alert.first_seen_at]));
    const blob = new Blob(["\ufeff", rows.map((row) => row.map(cell).join(",")).join("\r\n")], { type: "text/csv;charset=utf-8;" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `bugalert-eos-${new Date().toISOString().slice(0, 10)}.csv`;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    showToast(`Exported ${number(alerts.length)} alerts from the current filtered view.`);
  }

  const mobileQuery = matchMedia("(max-width: 760px)");
  function setSidebar(open) {
    const wasOpen = $("#sidebar").classList.contains("is-open");
    open = open && mobileQuery.matches;
    $("#sidebar").classList.toggle("is-open", open);
    $("#sidebar-scrim").hidden = !open;
    $("#menu-toggle").setAttribute("aria-expanded", String(open));
    $("#sidebar").inert = mobileQuery.matches && !open;
    $(".main-shell").inert = open;
    if (open) $("#sidebar [data-nav]").focus();
    else if (wasOpen) $("#menu-toggle").focus();
  }

  $$("[data-nav]").forEach((button) => button.addEventListener("click", () => {
    resetFilters();
    state.kind = button.dataset.nav === "security" ? "security" : "all";
    state.onlyNew = button.dataset.nav === "new";
    setSidebar(false);
    renderAlerts();
  }));
  $$("[data-kind]").forEach((button) => button.addEventListener("click", () => {
    state.kind = button.dataset.kind;
    state.page = 0;
    renderAlerts();
  }));
  $$("[data-stat]").forEach((button) => button.addEventListener("click", () => {
    resetFilters();
    if (button.dataset.stat === "security") state.kind = "security";
    if (button.dataset.stat === "severity") state.severity = "sev1";
    if (button.dataset.stat === "new") state.onlyNew = true;
    syncFilterControls();
    renderAlerts();
    $("#alert-register").scrollIntoView({ behavior: "smooth", block: "start" });
  }));
  $("#release-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-release]");
    if (!button) return;
    state.release = button.dataset.release;
    state.page = 0;
    syncFilterControls();
    setSidebar(false);
    renderAlerts();
  });
  $("#all-releases").addEventListener("click", () => {
    state.release = "all";
    state.page = 0;
    syncFilterControls();
    setSidebar(false);
    renderAlerts();
  });
  $("#search-input").addEventListener("input", (event) => {
    state.query = event.target.value;
    state.page = 0;
    renderAlerts();
  });
  [["#severity-filter", "severity"], ["#release-filter", "release"], ["#sort-select", "sort"]].forEach(([selector, key]) => {
    $(selector).addEventListener("change", (event) => {
      state[key] = event.target.value;
      state.page = 0;
      renderAlerts();
    });
  });
  $("#clear-filters").addEventListener("click", () => { resetFilters(); renderAlerts(); });
  $("#previous-page").addEventListener("click", () => {
    state.page = Math.max(0, state.page - 1);
    renderAlerts();
    $("#alert-register").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  $("#next-page").addEventListener("click", () => {
    state.page += 1;
    renderAlerts();
    $("#alert-register").scrollIntoView({ behavior: "smooth", block: "start" });
  });
  $("#refresh-button").addEventListener("click", refreshFeed);
  $("#export-button").addEventListener("click", exportCSV);
  $("#copy-alert-link").addEventListener("click", () => copyText(location.href));
  $("#copy-config-path").addEventListener("click", () => copyText(state.data?.config.path || "config.toml"));
  $("#close-toast").addEventListener("click", () => { $("#toast").hidden = true; });
  $("#menu-toggle").addEventListener("click", () => setSidebar(!$("#sidebar").classList.contains("is-open")));
  $("#sidebar-scrim").addEventListener("click", () => { setSidebar(false); $("#menu-toggle").focus(); });
  mobileQuery.addEventListener("change", () => setSidebar(false));
  $$("svg.icon").forEach((svg) => {
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
  });

  document.addEventListener("click", (event) => {
    if (event.target.closest("[data-open-config]")) openConfiguration();
    const close = event.target.closest("[data-close]");
    if (close) document.getElementById(close.dataset.close).close();
    if (event.target.closest("[data-reset-filters]")) { resetFilters(); renderAlerts(); }
    if (event.target.closest("#retry-dashboard")) void loadDashboard();
    const alert = event.target.closest("[data-alert]");
    if (alert && !getSelection()?.toString()) void openDetail(Number(alert.dataset.alert));
  });
  $$("dialog").forEach((dialog) => dialog.addEventListener("click", (event) => {
    if (event.target !== dialog) return;
    const bounds = dialog.getBoundingClientRect();
    if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();
  }));
  $("#detail-dialog").addEventListener("close", () => {
    state.detailId = null;
    detailController?.abort();
    const url = new URL(location.href);
    url.searchParams.delete("alert");
    history.replaceState(null, "", url);
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && $("#sidebar").classList.contains("is-open")) {
      setSidebar(false);
      $("#menu-toggle").focus();
    }
    if (event.key === "Tab" && $("#sidebar").classList.contains("is-open")) {
      const focusable = [...$("#sidebar").querySelectorAll("a[href], button:not(:disabled)")];
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
      if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
    }
    if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey
      && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)
      && !document.activeElement.isContentEditable && !$$("dialog[open]").length) {
      event.preventDefault();
      $("#search-input").focus();
    }
  });

  setSidebar(false);
  void loadDashboard();
  const events = new EventSource("/api/events");
  events.onopen = () => { state.connected = true; updateFeedStatus(); };
  events.addEventListener("update", () => { state.connected = true; void loadDashboard(); });
  events.onerror = () => { state.connected = false; updateFeedStatus(); };
  // A low-frequency fallback also recovers from proxies that buffer event streams.
  setInterval(() => { if (!document.hidden) void loadDashboard(); }, 30000);
  setInterval(updateFeedStatus, 1000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) void loadDashboard(); });
})();

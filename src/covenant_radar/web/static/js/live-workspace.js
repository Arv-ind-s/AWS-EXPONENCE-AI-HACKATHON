"use strict";

/* A small progressive enhancement: server data stays authoritative. */
(() => {
  const list = document.querySelector("[data-live-activity-list]");
  const toasts = document.querySelector("[data-live-toasts]");
  const drawer = document.querySelector("[data-live-activity-drawer]");
  const toggle = document.querySelector("[data-live-activity-toggle]");
  if (!list || !toasts || !drawer || !toggle || !window.fetch) return;

  let cursor = "";
  let pending = false;
  let timer = 0;
  let bootstrapped = false;
  const seen = new Set();
  const activity = [];
  const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const pause = () => document.hidden || document.body.dataset.overlayOpen === "true"
    || document.querySelector('form[data-dirty="true"], .row-select:checked') !== null;
  const cadence = () => document.getElementById("admin-ops-region") ? 15000
    : document.querySelector("#queue-ledger, #case-file") ? 30000 : 60000;
  const text = (value) => String(value || "").slice(0, 500);

  const itemNode = (item, toast = false) => {
    const node = document.createElement(item.deep_link ? "a" : "article");
    node.className = `live-activity-item live-activity-item--${item.severity || "info"}`;
    if (item.deep_link) {
      try {
        const url = new URL(item.deep_link, window.location.origin);
        if (url.origin === window.location.origin) node.href = `${url.pathname}${url.search}${url.hash}`;
      } catch (_error) {
        // A malformed optional link must not stop the entire activity stream.
      }
    }
    const title = document.createElement("strong");
    title.textContent = text(item.title);
    const body = document.createElement("span");
    body.textContent = text(item.body);
    const time = document.createElement("time");
    time.textContent = new Date(item.timestamp).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    node.append(title, body, time);
    if (toast) node.setAttribute("role", item.severity === "critical" ? "alert" : "status");
    return node;
  };
  const apply = (items) => {
    const fresh = items.filter((item) => item && item.id && !seen.has(item.id));
    items.forEach((item) => item && item.id && seen.add(item.id));
    fresh.forEach((item) => activity.unshift(item));
    if (fresh.length) {
      const unique = activity.filter((item, index, values) => values.findIndex((candidate) => candidate.id === item.id) === index);
      activity.splice(0, activity.length, ...unique.slice(0, 12));
      list.replaceChildren(...activity.map((item) => itemNode(item)));
    }
    if (!bootstrapped) { bootstrapped = true; return; }
    fresh.forEach((item) => {
      (item.affected_regions || []).forEach((id) => {
        const region = document.getElementById(id);
        if (!region) return;
        region.classList.add("live-region--changed");
        window.setTimeout(() => region.classList.remove("live-region--changed"), reduced ? 1 : 1400);
      });
      if (item.severity === "critical" || item.severity === "attention") {
        const toast = itemNode(item, true);
        toasts.append(toast);
        window.setTimeout(() => toast.remove(), reduced ? 4000 : 9000);
      }
    });
  };

  const poll = async () => {
    if (pending || pause()) return schedule();
    pending = true;
    try {
      const url = new URL("/live/updates", window.location.origin);
      if (cursor) url.searchParams.set("cursor", cursor);
      url.searchParams.set("context", window.location.pathname);
      const response = await fetch(url, { credentials: "same-origin", headers: { Accept: "application/json" } });
      if (response.status === 304) return;
      if (!response.ok) throw new Error("live updates unavailable");
      const envelope = await response.json();
      cursor = typeof envelope.cursor === "string" ? envelope.cursor : cursor;
      apply(Array.isArray(envelope.items) ? envelope.items : []);
      document.querySelectorAll("[data-live-status]").forEach((node) => { node.textContent = "Live"; });
    } catch (_error) {
      document.querySelectorAll("[data-live-status]").forEach((node) => { node.textContent = "Live updates reconnecting"; });
    } finally { pending = false; schedule(); }
  };
  const schedule = () => { window.clearTimeout(timer); timer = window.setTimeout(poll, cadence()); };

  toggle.addEventListener("click", () => { const open = drawer.hidden; drawer.hidden = !open; toggle.setAttribute("aria-expanded", String(open)); if (open) drawer.querySelector("button")?.focus(); });
  drawer.querySelector("[data-live-activity-close]")?.addEventListener("click", () => { drawer.hidden = true; toggle.setAttribute("aria-expanded", "false"); toggle.focus(); });
  document.addEventListener("visibilitychange", () => { if (!document.hidden) void poll(); });
  void poll();
})();

"use strict";

(() => {
  const root = document.documentElement;
  const focusableSelector = [
    "a[href]",
    "button:not([disabled])",
    "input:not([disabled])",
    "select:not([disabled])",
    "textarea:not([disabled])",
    "summary",
    "[tabindex]:not([tabindex='-1'])",
  ].join(",");

  const visibleFocusable = (container) => Array.from(container.querySelectorAll(focusableSelector))
    .filter((element) => !element.hidden && element.getClientRects().length > 0);

  const setOverlayState = () => {
    const drawerOpen = document.querySelector('[data-drawer][data-state="open"]');
    document.body.dataset.overlayOpen = drawerOpen || root.dataset.mobileNav === "open"
      ? "true"
      : "false";
  };

  const drawerFor = (id) => {
    if (!id) return null;
    const drawer = document.getElementById(id);
    return drawer && drawer.matches("[data-drawer]") ? drawer : null;
  };

  const openDrawer = (drawer, opener) => {
    drawer._radarOpener = opener;
    opener.setAttribute("aria-expanded", "true");
    drawer.hidden = false;
    drawer.dataset.state = "opening";
    drawer.setAttribute("aria-hidden", "false");
    window.requestAnimationFrame(() => {
      if (drawer.dataset.state === "opening") drawer.dataset.state = "open";
      setOverlayState();
    });
    const focusable = visibleFocusable(drawer);
    (focusable[0] || drawer).focus({ preventScroll: true });
  };

  const closeDrawer = (drawer) => {
    drawer.dataset.state = "closed";
    drawer.setAttribute("aria-hidden", "true");
    drawer.hidden = true;
    if (drawer._radarOpener && typeof drawer._radarOpener.focus === "function") {
      drawer._radarOpener.setAttribute("aria-expanded", "false");
      drawer._radarOpener.focus({ preventScroll: true });
    }
    drawer._radarOpener = null;
    setOverlayState();
  };

  const openMobileNavigation = (opener) => {
    root.dataset.mobileNav = "open";
    root._radarNavOpener = opener;
    opener.setAttribute("aria-expanded", "true");
    const sidebar = document.querySelector("[data-sidebar-panel]");
    const first = sidebar ? visibleFocusable(sidebar)[0] : null;
    if (first) first.focus({ preventScroll: true });
    setOverlayState();
  };

  const closeMobileNavigation = () => {
    if (root.dataset.mobileNav !== "open") return;
    delete root.dataset.mobileNav;
    if (root._radarNavOpener) {
      root._radarNavOpener.setAttribute("aria-expanded", "false");
      root._radarNavOpener.focus({ preventScroll: true });
    }
    root._radarNavOpener = null;
    setOverlayState();
  };

  const toggleSidebar = (button) => {
    const collapsed = root.dataset.sidebar === "collapsed";
    root.dataset.sidebar = collapsed ? "expanded" : "collapsed";
    button.setAttribute("aria-expanded", collapsed ? "true" : "false");
    try {
      window.localStorage.setItem("covenant-radar-sidebar", root.dataset.sidebar);
    } catch (_error) {
      // Storage can be disabled. The in-page state still works.
    }
  };

  const trapFocus = (event, container) => {
    if (event.key !== "Tab") return;
    const items = visibleFocusable(container);
    if (!items.length) {
      event.preventDefault();
      container.focus();
      return;
    }
    const first = items[0];
    const last = items[items.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const installQueueSelection = (scope = document) => {
    const ledger = scope.matches && scope.matches("#queue-ledger")
      ? scope
      : scope.querySelector && scope.querySelector("#queue-ledger");
    // The bar is a real form outside the table; the row boxes are bound to
    // it by `form="queue-selection"`, so a selection still posts with
    // script disabled.  This only manages its visibility and its count.
    const bar = document.getElementById("queue-selection");
    if (!ledger || !bar || ledger.dataset.selectionInstalled === "true") return;
    ledger.dataset.selectionInstalled = "true";

    const countDisplay = document.getElementById("selection-count");
    const selectAll = ledger.querySelector("#select-all");
    const clear = document.getElementById("clear-selection");
    // A row with no case carries a disabled box: it cannot be acted on, so
    // it must not be swept up by select-all either.
    const checkboxes = () => Array.from(ledger.querySelectorAll(".row-select:not([disabled])"));

    const update = () => {
      const rows = checkboxes();
      const checked = rows.filter((checkbox) => checkbox.checked);
      if (countDisplay) countDisplay.textContent = String(checked.length);
      bar.hidden = checked.length === 0;
      bar.dataset.selectionActive = checked.length ? "true" : "false";
      rows.forEach((checkbox) => {
        const row = checkbox.closest("tr");
        if (row) row.classList.toggle("ledger-row--selected", checkbox.checked);
      });
      if (selectAll) {
        selectAll.indeterminate = checked.length > 0 && checked.length < rows.length;
        selectAll.checked = rows.length > 0 && checked.length === rows.length;
      }
    };

    ledger.addEventListener("change", (event) => {
      const target = event.target;
      if (!(target instanceof HTMLInputElement)) return;
      if (target === selectAll) checkboxes().forEach((checkbox) => { checkbox.checked = target.checked; });
      if (target.matches("#select-all, .row-select")) update();
    });
    if (clear && clear.dataset.selectionInstalled !== "true") {
      clear.dataset.selectionInstalled = "true";
      clear.addEventListener("click", () => {
        checkboxes().forEach((checkbox) => { checkbox.checked = false; });
        update();
      });
    }
    update();
  };

  const detailRowFor = (button) => {
    const id = button.getAttribute("aria-controls");
    const row = id ? document.getElementById(id) : null;
    return row && row.matches(".ledger-detail") ? row : null;
  };

  const setRowExpanded = (button, expanded) => {
    const detail = detailRowFor(button);
    if (!detail) return;
    button.setAttribute("aria-expanded", expanded ? "true" : "false");
    detail.hidden = !expanded;
    const summaryRow = button.closest("tr");
    if (summaryRow) summaryRow.classList.toggle("ledger-row--expanded", expanded);
  };

  const installRowDisclosure = (scope = document) => {
    const ledger = scope.matches && scope.matches("#queue-ledger")
      ? scope
      : scope.querySelector && scope.querySelector("#queue-ledger");
    if (!ledger || ledger.dataset.disclosureInstalled === "true") return;
    ledger.dataset.disclosureInstalled = "true";
    // The server renders every detail row open so the screen is complete
    // without script; collapsing them is the enhancement, not the default.
    ledger.querySelectorAll(".ledger-row__disclosure").forEach((button) => {
      setRowExpanded(button, false);
    });
    ledger.addEventListener("click", (event) => {
      const button = event.target instanceof Element
        ? event.target.closest(".ledger-row__disclosure")
        : null;
      if (!button || !ledger.contains(button)) return;
      setRowExpanded(button, button.getAttribute("aria-expanded") !== "true");
    });
  };

  // A `[data-tabs]` container renders every panel in full, which is what a
  // reader without script gets and what several tests read out of the
  // response. With script the list of jump links at the top of it becomes a
  // tablist and only the selected panel stays visible. Panels are hidden,
  // never removed: `horizon.js` binds its controls once on load and finds
  // its chart by walking up to `[data-horizon-card]`, so a detached panel
  // would silently stop updating.
  const installTabStrip = (container) => {
    if (!container || container.dataset.tabsInstalled === "true") return;
    const list = container.querySelector("[data-tablist]");
    const tabs = list ? Array.from(list.querySelectorAll("[data-tab]")) : [];
    if (!list || tabs.length < 2) return;
    container.dataset.tabsInstalled = "true";

    const panelFor = (tab) => document.getElementById(tab.getAttribute("data-tab"));
    list.setAttribute("role", "tablist");
    tabs.forEach((tab) => {
      const target = panelFor(tab);
      if (!target) return;
      tab.setAttribute("role", "tab");
      tab.setAttribute("aria-controls", target.id);
      target.setAttribute("role", "tabpanel");
      target.setAttribute("aria-labelledby", tab.id || (tab.id = `${target.id}-tab`));
    });

    const select = (selected, { focus = false } = {}) => {
      tabs.forEach((tab) => {
        const target = panelFor(tab);
        const active = tab === selected;
        tab.setAttribute("aria-selected", active ? "true" : "false");
        // Only the selected tab stays in the tab order; the arrow keys move
        // between them, which is what a tablist is expected to do.
        tab.tabIndex = active ? 0 : -1;
        if (target) target.hidden = !active;
      });
      if (focus) selected.focus({ preventScroll: true });
    };

    list.addEventListener("click", (event) => {
      const tab = event.target instanceof Element
        ? event.target.closest("[data-tab]")
        : null;
      if (!tab || !list.contains(tab)) return;
      // The href is a real in-page link without script; with script it is a
      // tab, so it selects rather than jumping and moving the scroll.
      event.preventDefault();
      select(tab);
    });

    list.addEventListener("keydown", (event) => {
      const current = event.target instanceof Element
        ? event.target.closest("[data-tab]")
        : null;
      if (!current) return;
      const index = tabs.indexOf(current);
      const step = { ArrowRight: 1, ArrowLeft: -1, Home: -index, End: tabs.length - 1 - index };
      if (!(event.key in step)) return;
      event.preventDefault();
      select(tabs[(index + step[event.key] + tabs.length) % tabs.length], { focus: true });
    });

    select(tabs[0]);
  };

  // Innermost first, so a nested strip (the covenants inside the case file's
  // forecast tab) is wired up before its container hides it.
  const installTabs = (scope = document) => {
    if (!scope.querySelectorAll) return;
    Array.from(scope.querySelectorAll("[data-tabs]")).reverse().forEach(installTabStrip);
  };

  const expandedDetailIds = () => Array.from(
    document.querySelectorAll('.ledger-row__disclosure[aria-expanded="true"]'),
  ).map((button) => button.getAttribute("aria-controls")).filter(Boolean);

  const restoreExpandedDetails = (ids) => {
    if (!ids || !ids.length) return;
    ids.forEach((id) => {
      const button = document.querySelector(`.ledger-row__disclosure[aria-controls="${id}"]`);
      if (button) setRowExpanded(button, true);
    });
  };

  const csrfToken = () => {
    const field = document.querySelector('input[name="csrf_token"]');
    return field instanceof HTMLInputElement ? field.value : "";
  };

  const currentQueueFilters = () => {
    const form = document.getElementById("queue-filters");
    if (!(form instanceof HTMLFormElement)) return {};
    const filters = {};
    new FormData(form).forEach((value, key) => {
      const text = String(value).trim();
      // "All" is submitted as an empty value; a saved view records the
      // decisions the reader made, not the ones they left open.
      if (text && key !== "csrf_token") filters[key] = text;
    });
    return filters;
  };

  const installSavedViewSave = (scope = document) => {
    const region = scope.querySelector
      ? scope.querySelector("[data-save-view-region]")
      : null;
    if (!region || region.dataset.installed === "true") return;
    const toggle = region.querySelector("[data-save-view]");
    const form = region.querySelector("[data-save-view-form]");
    const status = region.querySelector("[data-save-view-status]");
    const name = region.querySelector("#save-view-name");
    if (!toggle || !(form instanceof HTMLFormElement) || !name) return;
    region.dataset.installed = "true";
    // Only now does the control become real: the API behind it needs script.
    region.hidden = false;

    toggle.addEventListener("click", () => {
      const opening = form.hidden;
      form.hidden = !opening;
      toggle.setAttribute("aria-expanded", opening ? "true" : "false");
      if (opening) name.focus();
    });

    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const label = name.value.trim();
      if (!label) return;
      if (status) status.textContent = "Saving…";
      try {
        const response = await fetch("/views", {
          method: "POST",
          credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            Accept: "application/json",
            "X-CSRF-Token": csrfToken(),
          },
          body: JSON.stringify({ name: label, kind: "queue", filters: currentQueueFilters() }),
        });
        if (!response.ok) {
          if (status) status.textContent = "That view could not be saved.";
          return;
        }
        if (status) status.textContent = `Saved “${label}”.`;
        name.value = "";
        form.hidden = true;
        toggle.setAttribute("aria-expanded", "false");
        // Re-read the workspace so the new view joins the picker without a
        // full page load, and without this handler guessing its URL.
        const workspace = document.getElementById("queue-workspace");
        if (workspace && window.htmx) {
          window.htmx.ajax("GET", window.location.href, {
            target: "#queue-workspace",
            swap: "outerHTML",
          });
        }
      } catch (_error) {
        if (status) status.textContent = "That view could not be saved.";
      }
    });
  };

  const markForms = (scope = document) => {
    const forms = scope.querySelectorAll ? scope.querySelectorAll("form") : [];
    forms.forEach((form) => {
      if (form.dataset.enhanced === "true") return;
      form.dataset.enhanced = "true";
      const initial = new FormData(form);
      const signature = () => Array.from(new FormData(form).entries())
        .map(([key, value]) => `${key}:${value instanceof File ? value.name : String(value)}`)
        .join("|");
      form.dataset.initialSignature = Array.from(initial.entries())
        .map(([key, value]) => `${key}:${value instanceof File ? value.name : String(value)}`)
        .join("|");
      form.addEventListener("input", () => {
        if (form.method.toLowerCase() === "get") return;
        form.dataset.dirty = signature() === form.dataset.initialSignature ? "false" : "true";
      });
    });
  };

  const enhanceUploads = (scope = document) => {
    const uploads = scope.querySelectorAll ? scope.querySelectorAll(".intake-upload") : [];
    uploads.forEach((upload) => {
      if (upload.dataset.dropInstalled === "true") return;
      const input = upload.querySelector('input[type="file"]');
      if (!input) return;
      upload.dataset.dropInstalled = "true";
      ["dragenter", "dragover"].forEach((type) => upload.addEventListener(type, (event) => {
        event.preventDefault();
        upload.dataset.dragActive = "true";
      }));
      ["dragleave", "drop"].forEach((type) => upload.addEventListener(type, () => {
        upload.dataset.dragActive = "false";
      }));
      upload.addEventListener("drop", (event) => {
        event.preventDefault();
        if (event.dataTransfer && event.dataTransfer.files.length) {
          input.files = event.dataTransfer.files;
          input.dispatchEvent(new Event("change", { bubbles: true }));
        }
      });
    });
  };

  const installLiveSearch = (scope = document) => {
    const inputs = scope.querySelectorAll ? scope.querySelectorAll("[data-live-search]") : [];
    inputs.forEach((input) => {
      if (input.dataset.liveSearchInstalled === "true") return;
      const form = input.closest("form");
      if (!(form instanceof HTMLFormElement)) return;
      input.dataset.liveSearchInstalled = "true";
      let timer = 0;
      const submit = () => {
        window.clearTimeout(timer);
        timer = window.setTimeout(() => form.requestSubmit(), 300);
      };
      input.addEventListener("input", submit);
      input.addEventListener("search", () => {
        window.clearTimeout(timer);
        form.requestSubmit();
      });
    });
  };

  const installEnhancements = (scope = document) => {
    installQueueSelection(scope);
    installRowDisclosure(scope);
    installTabs(scope);
    installSavedViewSave(scope);
    markForms(scope);
    enhanceUploads(scope);
    installLiveSearch(scope);
    installQueueUpdateNotice(scope);
  };

  const pollingShouldPause = (element) => {
    if (document.hidden || document.body.dataset.overlayOpen === "true") return true;
    if (element.getAttribute("aria-busy") === "true") return true;
    if (element.dataset.pollState === "failed") return true;
    if (document.querySelector(".row-select:checked")) return true;
    const region = element.closest("main") || document;
    return Boolean(region.querySelector('form[data-dirty="true"]'));
  };

  const showPollRetry = (element) => {
    if (element.querySelector("[data-poll-retry]")) return;
    const notice = document.createElement("div");
    notice.className = "poll-retry state state--degraded";
    notice.setAttribute("role", "status");
    const message = document.createElement("span");
    message.textContent = "Live updates paused after repeated connection failures.";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "button button--secondary";
    button.dataset.pollRetry = "true";
    button.textContent = "Retry live updates";
    notice.append(message, button);
    element.append(notice);
  };

  const clearPollRetry = (element) => {
    const notice = element.querySelector("[data-poll-retry]")?.closest(".poll-retry");
    if (notice) notice.remove();
  };

  const installQueueUpdateNotice = (scope = document) => {
    const notice = scope.querySelector ? scope.querySelector("[data-queue-update-notice]") : null;
    if (!notice || notice.dataset.installed === "true") return;
    const refresh = notice.querySelector("[data-queue-update-refresh]");
    if (!refresh) return;
    notice.dataset.installed = "true";
    refresh.addEventListener("click", () => {
      const ledger = document.getElementById("queue-ledger");
      notice.hidden = true;
      if (ledger && window.htmx) {
        // This is an analyst-approved refresh. Let the next response replace
        // the stale ledger even when its run id differs from the passive poll.
        ledger.dataset.allowRunSwap = "true";
        window.htmx.trigger(ledger, "refresh");
      }
    });
  };

  let swapSnapshot = null;

  document.addEventListener("click", (event) => {
    const target = event.target;
    if (!(target instanceof Element)) return;

    const userMenuToggle = target.closest("[data-user-menu-toggle]");
    if (userMenuToggle) {
      const menu = userMenuToggle.closest("[data-user-menu]");
      const panel = menu && menu.querySelector(".shell-user-menu__panel");
      if (menu && panel) {
        const opening = menu.dataset.state !== "open";
        menu.dataset.state = opening ? "open" : "closed";
        panel.hidden = !opening;
        userMenuToggle.setAttribute("aria-expanded", opening ? "true" : "false");
        document.body.dataset.overlayOpen = opening ? "true" : "false";
      }
      return;
    }
    const openUserMenu = document.querySelector('[data-user-menu][data-state="open"]');
    if (openUserMenu && !target.closest("[data-user-menu]")) {
      openUserMenu.dataset.state = "closed";
      const panel = openUserMenu.querySelector(".shell-user-menu__panel");
      const toggle = openUserMenu.querySelector("[data-user-menu-toggle]");
      if (panel) panel.hidden = true;
      if (toggle) toggle.setAttribute("aria-expanded", "false");
      setOverlayState();
    }

    const passwordToggle = target.closest("[data-password-toggle]");
    if (passwordToggle) {
      const input = document.getElementById(passwordToggle.dataset.passwordToggle || "");
      if (input instanceof HTMLInputElement) {
        const reveal = input.type === "password";
        input.type = reveal ? "text" : "password";
        passwordToggle.setAttribute("aria-pressed", reveal ? "true" : "false");
        passwordToggle.textContent = reveal ? "Hide" : "Show";
        input.focus({ preventScroll: true });
      }
      return;
    }

    const sidebarToggle = target.closest("[data-sidebar-toggle]");
    if (sidebarToggle) {
      toggleSidebar(sidebarToggle);
      return;
    }
    const sidebarOpen = target.closest("[data-sidebar-open]");
    if (sidebarOpen) {
      openMobileNavigation(sidebarOpen);
      return;
    }
    if (target.closest("[data-sidebar-close]")) {
      closeMobileNavigation();
      return;
    }

    const opener = target.closest("[data-drawer-open]");
    if (opener) {
      const drawer = drawerFor(opener.getAttribute("data-drawer-open"));
      if (drawer) {
        event.preventDefault();
        openDrawer(drawer, opener);
      }
      return;
    }
    const closer = target.closest("[data-drawer-close]");
    if (closer) {
      const drawer = drawerFor(closer.getAttribute("data-drawer-close"));
      if (drawer) {
        event.preventDefault();
        closeDrawer(drawer);
      }
      return;
    }

    const dismiss = target.closest("[data-toast-dismiss]");
    if (dismiss) {
      const toast = document.getElementById(dismiss.getAttribute("data-toast-dismiss"));
      if (toast && toast.matches("[data-toast]")) {
        toast.hidden = true;
        toast.dataset.dismissed = "true";
      }
      return;
    }

    const retry = target.closest("[data-poll-retry]");
    if (retry && window.htmx) {
      const poll = retry.closest("[data-live-poll]");
      if (poll) {
        poll.dataset.pollFailures = "0";
        poll.dataset.pollState = "active";
        clearPollRetry(poll);
        window.htmx.trigger(poll, "refresh");
      }
    }
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !event.ctrlKey && !event.metaKey && !event.altKey) {
      const active = document.activeElement;
      if (!(active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement || active instanceof HTMLSelectElement)) {
        const search = document.getElementById("shell-search-query");
        if (search) {
          event.preventDefault();
          search.focus();
        }
      }
    }

    const openUserMenu = document.querySelector('[data-user-menu][data-state="open"]');
    if (openUserMenu && event.key === "Escape") {
      const panel = openUserMenu.querySelector(".shell-user-menu__panel");
      const toggle = openUserMenu.querySelector("[data-user-menu-toggle]");
      openUserMenu.dataset.state = "closed";
      if (panel) panel.hidden = true;
      if (toggle) {
        toggle.setAttribute("aria-expanded", "false");
        toggle.focus({ preventScroll: true });
      }
      setOverlayState();
      return;
    }

    const openDrawerElement = document.querySelector('[data-drawer][data-state="open"]');
    if (openDrawerElement) {
      if (event.key !== "Escape") trapFocus(event, openDrawerElement);
      else closeDrawer(openDrawerElement);
      return;
    }
    if (root.dataset.mobileNav === "open") {
      const sidebar = document.querySelector("[data-sidebar-panel]");
      if (event.key === "Escape") closeMobileNavigation();
      else if (sidebar) trapFocus(event, sidebar);
    }
  });

  document.addEventListener("submit", (event) => {
    const form = event.target;
    if (!(form instanceof HTMLFormElement)) return;
    if (form.matches("[data-filter-form]")) {
      const state = form.dataset.state;
      if (state === "rest" || state === "saved-view") form.dataset.state = "active";
    }
    form.setAttribute("aria-busy", "true");
    const submitter = event.submitter;
    if (submitter instanceof HTMLButtonElement || submitter instanceof HTMLInputElement) {
      submitter.setAttribute("aria-busy", "true");
      submitter.classList.add("button--loading");
    }
  });

  document.addEventListener("change", (event) => {
    const target = event.target;
    if (!(target instanceof HTMLInputElement || target instanceof HTMLSelectElement)) return;
    const form = target.closest("[data-filter-form][data-submit-on-change]");
    if (form instanceof HTMLFormElement) form.requestSubmit();
  });

  document.addEventListener("htmx:beforeRequest", (event) => {
    const element = event.detail && event.detail.elt;
    if (!(element instanceof Element) || !element.matches("[data-live-poll]")) return;
    if (pollingShouldPause(element)) {
      event.preventDefault();
      if (element.dataset.pollState !== "failed") element.dataset.pollState = "paused";
    } else {
      element.dataset.pollState = "active";
    }
  });

  document.addEventListener("htmx:beforeSwap", (event) => {
    const xhr = event.detail && event.detail.xhr;
    if (!xhr || !xhr.responseURL) return;
    const target = event.detail && event.detail.target;
    const requestElement = event.detail && event.detail.elt;
    if (target instanceof Element && target.id === "queue-ledger"
        && requestElement instanceof Element && requestElement === target) {
      const match = xhr.responseText.match(/data-run-id="([^"]*)"/);
      const incomingRun = match ? match[1] : "";
      const currentRun = target.dataset.runId || "";
      const allowRunSwap = target.dataset.allowRunSwap === "true";
      delete target.dataset.allowRunSwap;
      if (!allowRunSwap && incomingRun && currentRun && incomingRun !== currentRun) {
        event.preventDefault();
        const notice = document.querySelector("[data-queue-update-notice]");
        if (notice) notice.hidden = false;
        return;
      }
    }
    try {
      if (new URL(xhr.responseURL).pathname === "/sign-in") {
        event.preventDefault();
        window.location.assign("/sign-in");
      }
    } catch (_error) {
      // A malformed response URL is handled by HTMX's normal error path.
    }
    const active = document.activeElement;
    swapSnapshot = {
      focusId: active instanceof HTMLElement ? active.id : "",
      scrollX: window.scrollX,
      scrollY: window.scrollY,
      // An analyst reading a detail row should still receive the poll, so the
      // rows are re-opened after the swap rather than the poll being paused.
      expanded: expandedDetailIds(),
    };
  });

  document.addEventListener("htmx:afterRequest", (event) => {
    const element = event.detail && event.detail.elt;
    if (!(element instanceof Element)) return;
    const form = element instanceof HTMLFormElement ? element : element.closest("form");
    if (form) form.removeAttribute("aria-busy");
    element.querySelectorAll?.('[aria-busy="true"]').forEach((item) => {
      item.removeAttribute("aria-busy");
      item.classList.remove("button--loading");
    });
    if (!element.matches("[data-live-poll]")) return;
    if (event.detail.successful) {
      element.dataset.pollFailures = "0";
      element.dataset.pollState = "active";
      clearPollRetry(element);
      const timestamp = element.querySelector("[data-last-updated]");
      if (timestamp) {
        const now = new Date();
        timestamp.dateTime = now.toISOString();
        timestamp.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
      }
      return;
    }
    const failures = Number(element.dataset.pollFailures || "0") + 1;
    element.dataset.pollFailures = String(failures);
    if (failures >= 3) {
      element.dataset.pollState = "failed";
      showPollRetry(element);
    }
  });

  document.addEventListener("htmx:afterSwap", (event) => {
    const target = event.detail && event.detail.target ? event.detail.target : document;
    installEnhancements(target);
    if (target instanceof Element && target.id === "case-memo") {
      // A memo response replaces the target below the pressed button. Generic
      // swap restoration intentionally keeps polling tables still, but doing
      // that here made a successful model call look like it returned nothing.
      // Move both the viewport and assistive-technology focus to the freshly
      // rendered explanation.
      swapSnapshot = null;
      const explanation = document.getElementById("case-memo");
      if (explanation instanceof HTMLElement) {
        explanation.focus({ preventScroll: true });
        explanation.scrollIntoView({ behavior: "smooth", block: "start" });
      }
    }
    if (target instanceof Element && target.id === "queue-ledger") {
      const notice = document.querySelector("[data-queue-update-notice]");
      if (notice) notice.hidden = true;
    }
    const timestamp = target.querySelector && target.querySelector("[data-last-updated]");
    if (timestamp) {
      const now = new Date();
      timestamp.dateTime = now.toISOString();
      timestamp.textContent = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    if (swapSnapshot) {
      restoreExpandedDetails(swapSnapshot.expanded);
      const focusTarget = swapSnapshot.focusId && document.getElementById(swapSnapshot.focusId);
      if (focusTarget) focusTarget.focus({ preventScroll: true });
      window.scrollTo(swapSnapshot.scrollX, swapSnapshot.scrollY);
      swapSnapshot = null;
    }
  });

  document.addEventListener("DOMContentLoaded", () => installEnhancements(document));
})();

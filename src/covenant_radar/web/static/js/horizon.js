"use strict";

/*
 * The horizon control is a small, dependency-free state machine.  Its only
 * business data comes from C-03; the browser never projects a missing day or
 * calculates a probability.  The default DOM state is the stop-link fallback,
 * so disabling JavaScript cannot remove the meaning of the screen.
 */
(() => {
  const REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";
  const SVG_NAMESPACE = "http://www.w3.org/2000/svg";
  const DECIMAL_PATTERN = /^-?(?:\d+|\d*\.\d+)(?:[eE][+-]?\d+)?$/;
  const DATE_PATTERN = /^(\d{4})-(\d{2})-(\d{2})$/;
  const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
  const DAY_MS = 86400000;

  const parseInteger = (value) => {
    if (typeof value !== "string" && typeof value !== "number") return null;
    const text = String(value);
    if (!/^\d+$/.test(text)) return null;
    const parsed = Number(text);
    return Number.isSafeInteger(parsed) ? parsed : null;
  };

  const rangeFor = (control) => {
    const minimum = parseInteger(control.dataset.minimumDay);
    const maximum = parseInteger(control.dataset.maximumDay);
    if (minimum === null || maximum === null || maximum < minimum) return null;
    return { minimum, maximum };
  };

  const clampDay = (day, range) => Math.min(range.maximum, Math.max(range.minimum, day));

  const selectedDayFor = (control, range) => {
    const selected = parseInteger(control.dataset.selectedDay);
    return clampDay(selected === null ? range.minimum : selected, range);
  };

  const setText = (element, value) => {
    if (element) element.textContent = value;
  };

  const setSelectedDay = (state, day) => {
    state.input.value = String(day);
    state.input.setAttribute("aria-valuenow", String(day));
    state.control.dataset.selectedDay = String(day);
    setText(state.control.querySelector("[data-horizon-value]"), String(day));
    state.control.querySelectorAll("[data-horizon-stop]").forEach((stop) => {
      stop.setAttribute(
        "aria-current",
        stop.dataset.horizonStop === String(day) ? "true" : "false",
      );
      if (stop.dataset.horizonStop !== String(day)) stop.removeAttribute("aria-current");
    });
  };

  const apiUrlFor = (state, day) => {
    try {
      const url = new URL(state.control.dataset.horizonApi || "", window.location.origin);
      if (url.origin !== window.location.origin) return null;
      url.searchParams.set("day", String(day));
      return url;
    } catch (_error) {
      return null;
    }
  };

  const validDecimal = (value) => {
    if (typeof value !== "string" && typeof value !== "number") return null;
    const text = String(value);
    if (!DECIMAL_PATTERN.test(text)) return null;
    const numeric = Number(text);
    return Number.isFinite(numeric) ? text : null;
  };

  const decimalText = (value) => {
    const text = validDecimal(value);
    if (text === null) return "Unavailable";
    if (!text.includes(".")) return text;
    return text.replace(/(\.\d*?[1-9])0+(?:$|e)/i, "$1").replace(/\.0+(?:$|e)/i, "");
  };

  const percentageText = (value) => {
    const text = validDecimal(value);
    if (text === null) return "Unavailable";
    const numeric = Number(text);
    if (numeric < 0 || numeric > 1) return "Unavailable";
    return `${Math.round(numeric * 100)}%`;
  };

  const dateParts = (value) => {
    if (typeof value !== "string") return null;
    const match = DATE_PATTERN.exec(value);
    if (!match) return null;
    const year = Number(match[1]);
    const month = Number(match[2]);
    const day = Number(match[3]);
    const timestamp = Date.UTC(year, month - 1, day);
    const candidate = new Date(timestamp);
    if (
      !Number.isFinite(timestamp) ||
      candidate.getUTCFullYear() !== year ||
      candidate.getUTCMonth() !== month - 1 ||
      candidate.getUTCDate() !== day
    ) {
      return null;
    }
    return { year, month, day, timestamp };
  };

  const displayDate = (value) => {
    const parts = dateParts(value);
    if (parts === null) return null;
    const locale = document.documentElement.lang === "hi" ? "hi-IN" : "en-IN";
    return new Intl.DateTimeFormat(locale, {
      day: "2-digit",
      month: "short",
      year: "numeric",
      timeZone: "UTC",
    }).format(new Date(parts.timestamp));
  };

  const crossingDayFor = (state, crossingDate) => {
    const crossing = dateParts(crossingDate);
    const asOf = dateParts(state.control.dataset.asOfDate);
    if (crossing === null || asOf === null) return null;
    const day = Math.round((crossing.timestamp - asOf.timestamp) / DAY_MS);
    return Number.isSafeInteger(day) && day >= 0 ? day : null;
  };

  const validPayload = (payload, requestedDay) => {
    if (!payload || typeof payload !== "object" || Array.isArray(payload)) return false;
    if (parseInteger(payload.day) !== requestedDay) return false;
    for (const field of [
      "projected_value",
      "headroom_pct",
      "probability",
      "confidence",
      "crossing_date",
    ]) {
      if (!(field in payload)) return false;
    }
    return (
      typeof payload.below_confidence_floor === "boolean" &&
      Array.isArray(payload.drivers)
    );
  };

  const createSvgElement = (name, attributes) => {
    const element = document.createElementNS(SVG_NAMESPACE, name);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, String(value)));
    return element;
  };

  const plotGeometry = (svg) => {
    const threshold = svg.querySelector(".trajectory__threshold");
    const xStart = Number(threshold?.getAttribute("x1"));
    const xEnd = Number(threshold?.getAttribute("x2"));
    // The threshold is a horizontal rule, so its two y values are identical.
    // Taking the plot's vertical extent from it gave every marker below a
    // height of zero, which is why the selected-day line never appeared and
    // why the server's crossing tick vanished the moment the slider moved:
    // `clearDynamicMarkers` removed it and this redrew it invisible.  The
    // renderer now states the real extent on the root element; the threshold
    // stays as the fallback for any chart rendered before it did.
    const top = Number(svg.getAttribute("data-plot-top"));
    const bottom = Number(svg.getAttribute("data-plot-bottom"));
    const hasBounds = Number.isFinite(top) && Number.isFinite(bottom) && bottom > top;
    const yStart = hasBounds ? top : Number(threshold?.getAttribute("y1"));
    const yEnd = hasBounds ? bottom : Number(threshold?.getAttribute("y2"));
    if (![xStart, xEnd, yStart, yEnd].every(Number.isFinite) || xEnd <= xStart) return null;
    return { xStart, xEnd, yStart: Math.min(yStart, yEnd), yEnd: Math.max(yStart, yEnd) };
  };

  const clearDynamicMarkers = (svg) => {
    svg
      .querySelectorAll(
        "[data-horizon-selected-marker], [data-horizon-crossing-marker], " +
          ".trajectory__crossing-tick, .trajectory__crossing",
      )
      .forEach((marker) => marker.remove());
  };

  const updateTrajectory = (state, payload, day) => {
    const svg = state.card.querySelector("[data-trajectory=\"stored\"]");
    if (!svg) return;
    const geometry = plotGeometry(svg);
    const line = svg.querySelector(".trajectory__line");
    if (!geometry || !line) return;

    clearDynamicMarkers(svg);
    const denominator = state.range.maximum - state.range.minimum;
    const progress = denominator === 0
      ? 0
      : (day - state.range.minimum) / denominator;
    const x = geometry.xStart + (geometry.xEnd - geometry.xStart) * progress;
    const selectedMarker = createSvgElement("line", {
      class: "trajectory__selected-day",
      x1: x,
      y1: geometry.yStart,
      x2: x,
      y2: geometry.yEnd,
      "data-horizon-selected-marker": "true",
      "data-selected-day": day,
    });
    svg.appendChild(selectedMarker);

    // The path is already a persisted path rendered by the server.  Clipping
    // it at the response's selected day makes the current state visible
    // without deriving or filling any missing business value in the browser.
    // The area fill is the same path closed to the plot floor and so shares
    // the polyline's horizontal extent exactly; the one percentage clips
    // both at the same day.
    const rightInset = Math.max(0, 100 - progress * 100);
    const clip = `inset(0 ${rightInset}% 0 0)`;
    line.style.clipPath = clip;
    const area = svg.querySelector(".trajectory__area");
    if (area) area.style.clipPath = clip;
    line.dataset.selectedDay = String(day);
    const crossingDate = displayDate(payload.crossing_date);
    const crossingDay = crossingDate === null ? null : crossingDayFor(state, payload.crossing_date);
    if (crossingDate !== null && crossingDay !== null && day >= crossingDay) {
      const crossingProgress = denominator === 0
        ? 0
        : (crossingDay - state.range.minimum) / denominator;
      const crossingX =
        geometry.xStart + (geometry.xEnd - geometry.xStart) * crossingProgress;
      svg.appendChild(
        createSvgElement("line", {
          class: "trajectory__crossing-tick",
          x1: crossingX,
          y1: geometry.yStart,
          x2: crossingX,
          y2: geometry.yEnd,
          "data-horizon-crossing-marker": "true",
          "data-crossing-day": crossingDay,
          "data-crossing-date": payload.crossing_date,
        }),
      );
    }
  };

  const updateDrivers = (state, payload) => {
    const list = state.control.querySelector("[data-horizon-drivers]");
    if (!list) return;
    list.replaceChildren();
    if (payload.drivers.length === 0) {
      const empty = document.createElement("li");
      empty.textContent = "No driver record is stored for this selected day.";
      list.appendChild(empty);
      return;
    }
    payload.drivers.forEach((driver) => {
      if (!driver || typeof driver.name !== "string" || !driver.name.trim()) return;
      const item = document.createElement("li");
      item.textContent = driver.name.trim();
      if (validDecimal(driver.share) !== null) {
        const share = document.createElement("span");
        share.dataset.driverShare = "true";
        share.textContent = ` (${percentageText(driver.share)})`;
        item.appendChild(share);
      }
      if (typeof driver.evidence_id === "string" && UUID_PATTERN.test(driver.evidence_id)) {
        const citation = document.createElement("a");
        citation.href = `#evidence-item-${driver.evidence_id}`;
        citation.textContent = " View cited evidence";
        item.appendChild(citation);
      }
      list.appendChild(item);
    });
    if (list.children.length === 0) {
      const empty = document.createElement("li");
      empty.textContent = "No driver record is stored for this selected day.";
      list.appendChild(empty);
    }
  };

  const updateHeader = (state, payload) => {
    if (state.card.dataset.horizonUpdatesHeader !== "true") return;
    const header = document.querySelector("[data-horizon-header-risk]");
    if (!header) return;
    const date = displayDate(payload.crossing_date);
    if (date === null) {
      setText(
        header,
        `No projected crossing in the stored horizon; ${state.control.dataset.directionLabel || "direction is not recorded"}.`,
      );
      return;
    }
    const crossingDay = crossingDayFor(state, payload.crossing_date);
    setText(
      header,
      crossingDay !== null && state.appliedDay >= crossingDay
        ? `Crossing: ${date}`
        : `Projected crossing: ${date}`,
    );
  };

  const applyPayload = (state, payload, day) => {
    const unit = state.card.dataset.unit || "";
    const projected = payload.projected_value === null
      ? "Value unavailable"
      : `${decimalText(payload.projected_value)}${unit}`;
    const headroom = payload.headroom_pct === null
      ? "Headroom unavailable"
      : `${decimalText(payload.headroom_pct)}%`;
    let probability = "No probability is recorded for this selected day.";
    if (payload.below_confidence_floor) {
      probability = "Suppressed — confidence is below the display floor.";
    } else if (payload.probability !== null) {
      probability = percentageText(payload.probability);
    }
    const confidence = payload.confidence === null
      ? "Confidence unavailable"
      : percentageText(payload.confidence);
    const crossingDate = displayDate(payload.crossing_date);
    const crossingDay = crossingDate === null ? null : crossingDayFor(state, payload.crossing_date);
    const crossing = crossingDate === null
      ? `No projected crossing in the stored horizon; ${state.control.dataset.directionLabel || "direction is not recorded"}.`
      : crossingDay !== null && day >= crossingDay
        ? `Crossing: ${crossingDate}`
        : `Projected crossing: ${crossingDate}`;

    setText(state.control.querySelector("[data-horizon-projected-value]"), projected);
    setText(state.control.querySelector("[data-horizon-headroom]"), headroom);
    const probabilityElement = state.control.querySelector("[data-horizon-probability]");
    setText(probabilityElement, probability);
    if (probabilityElement) {
      probabilityElement.dataset.suppressed = payload.below_confidence_floor ? "true" : "false";
    }
    setText(state.control.querySelector("[data-horizon-confidence]"), confidence);
    setText(state.control.querySelector("[data-horizon-crossing]"), crossing);
    updateTrajectory(state, payload, day);
    updateDrivers(state, payload);
    updateHeader(state, payload);
  };

  const setStatus = (state, message, kind = "ready") => {
    state.control.dataset.state = kind;
    setText(state.control.querySelector("[data-horizon-status]"), message);
  };

  const requestDay = async (state) => {
    if (state.pending || state.queuedDay === null || !state.interactive) return;
    const day = state.queuedDay;
    state.queuedDay = null;
    if (day === state.appliedDay) return;
    const url = apiUrlFor(state, day);
    if (url === null) {
      setStatus(state, "The stored forecast path is unavailable; named stops remain available.", "error");
      return;
    }

    state.pending = true;
    state.control.setAttribute("aria-busy", "true");
    setStatus(state, "Loading the selected stored day; the previous value remains visible.", "loading");
    try {
      const response = await fetch(url, {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error(`forecast path request failed with ${response.status}`);
      const payload = await response.json();
      if (!validPayload(payload, day)) throw new Error("forecast path response failed validation");
      state.appliedDay = day;
      applyPayload(state, payload, day);
      setStatus(state, `Stored forecast day ${day} loaded.`, "ready");
    } catch (_error) {
      setStatus(state, "The selected stored day could not be loaded; the previous value remains visible.", "error");
    } finally {
      state.pending = false;
      state.control.removeAttribute("aria-busy");
      if (state.queuedDay !== null && state.queuedDay !== state.appliedDay) {
        void requestDay(state);
      } else if (state.control.dataset.state === "loading") {
        setStatus(state, "The previous stored value remains visible.", "ready");
      }
    }
  };

  const queueDay = (state, rawDay) => {
    const parsed = parseInteger(rawDay);
    if (parsed === null) return;
    const day = clampDay(parsed, state.range);
    setSelectedDay(state, day);
    state.queuedDay = day;
    void requestDay(state);
  };

  const setMode = (state, reduced) => {
    const wasInteractive = state.interactive;
    state.interactive = !reduced && typeof window.fetch === "function";
    state.control.dataset.horizonMode = state.interactive ? "interactive" : "stops";
    if (state.interactive && !wasInteractive && state.queuedDay !== null) {
      void requestDay(state);
    }
  };

  const install = (control) => {
    const card = control.closest("[data-horizon-card]");
    const input = control.querySelector("[data-horizon-input]");
    const range = rangeFor(control);
    if (!card || !input || !range) return;

    const state = {
      control,
      card,
      input,
      range,
      pending: false,
      queuedDay: null,
      appliedDay: null,
      interactive: false,
    };
    const selected = selectedDayFor(control, range);
    setSelectedDay(state, selected);
    control.dataset.horizonJs = "true";
    const motion = typeof window.matchMedia === "function"
      ? window.matchMedia(REDUCED_MOTION_QUERY)
      : { matches: false };
    setMode(state, Boolean(motion.matches));

    input.addEventListener("input", () => {
      if (!state.interactive) return;
      queueDay(state, input.value);
    });
    input.addEventListener("keydown", (event) => {
      if (!state.interactive) return;
      const current = parseInteger(input.value);
      if (current === null) return;
      let target = null;
      if (event.key === "ArrowRight" || event.key === "ArrowUp") {
        target = current + (event.shiftKey ? 7 : 1);
      } else if (event.key === "ArrowLeft" || event.key === "ArrowDown") {
        target = current - (event.shiftKey ? 7 : 1);
      } else if (event.key === "Home") {
        target = range.minimum;
      } else if (event.key === "End") {
        target = range.maximum;
      }
      if (target === null) return;
      event.preventDefault();
      queueDay(state, target);
    });
    control.querySelectorAll("[data-horizon-stop]").forEach((stop) => {
      stop.addEventListener("click", (event) => {
        if (!state.interactive) return;
        event.preventDefault();
        queueDay(state, stop.dataset.horizonStop);
      });
    });

    if (typeof motion.addEventListener === "function") {
      motion.addEventListener("change", (event) => setMode(state, event.matches));
    } else if (typeof motion.addListener === "function") {
      motion.addListener((event) => setMode(state, event.matches));
    }

    if (state.interactive) {
      state.queuedDay = selected;
      void requestDay(state);
    }
  };

  document.querySelectorAll("[data-horizon-control]").forEach(install);
})();

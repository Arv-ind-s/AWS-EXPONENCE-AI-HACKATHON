"use strict";

(() => {
  document.addEventListener("DOMContentLoaded", () => {
    const mark = document.querySelector("[data-span-highlight]");
    if (mark) mark.scrollIntoView({ block: "center" });
  });
})();

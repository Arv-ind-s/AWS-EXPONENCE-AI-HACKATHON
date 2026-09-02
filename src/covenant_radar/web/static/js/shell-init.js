"use strict";

(() => {
  const root = document.documentElement;
  root.classList.remove("no-js");
  root.classList.add("js");
  try {
    root.dataset.sidebar = window.localStorage.getItem("covenant-radar-sidebar") === "collapsed"
      ? "collapsed"
      : "expanded";
  } catch (_error) {
    root.dataset.sidebar = "expanded";
  }
})();

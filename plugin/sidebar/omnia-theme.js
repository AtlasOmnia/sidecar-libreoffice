// SPDX-License-Identifier: Apache-2.0
// Copyright 2026 Atlas Omnia
//
// Palette and appearance for the Sidecar window. Plain-JS port of AtlasOmnia/sidecar
// excel/src/taskpane/omniaTheme.ts with the same storage key ("omnia.theme"), the same palette and
// mode values and the same resolve rules. The one difference: "Auto" follows LibreOffice's
// appearance, which the page receives as host_scheme in the session exchange (never from the URL),
// instead of Office.context.officeTheme. localStorage holds only this theme preference.
(function () {
  "use strict";
  var THEME_KEY = "omnia.theme";
  var PALETTES = ["teal", "blue", "gold", "red"];
  var MODES = ["auto", "dark", "light"];
  var hostScheme = null;

  function normalizeTheme(value) {
    var v = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    return {
      palette: PALETTES.indexOf(v.palette) >= 0 ? v.palette : "teal",
      mode: MODES.indexOf(v.mode) >= 0 ? v.mode : "auto",
    };
  }
  function storage() {
    try { return window.localStorage; } catch (e) { return undefined; }
  }
  function loadTheme() {
    try {
      var store = storage();
      return normalizeTheme(JSON.parse((store && store.getItem(THEME_KEY)) || "null"));
    } catch (e) {
      return normalizeTheme(null);
    }
  }
  function saveTheme(value) {
    try {
      var store = storage();
      if (!store) return false;
      store.setItem(THEME_KEY, JSON.stringify(normalizeTheme(value)));
      return true;
    } catch (e) {
      return false;
    }
  }
  function media() {
    return typeof window.matchMedia === "function" ? window.matchMedia("(prefers-color-scheme: dark)") : undefined;
  }
  function resolveScheme(mode) {
    if (mode !== "auto") return mode;
    if (hostScheme === "dark" || hostScheme === "light") return hostScheme;
    var m = media();
    return m && m.matches ? "dark" : "light";
  }
  function applyTheme(root, prefs) {
    root.dataset.palette = prefs.palette;
    root.dataset.scheme = resolveScheme(prefs.mode);
  }

  // Apply before first paint: this file is a synchronous script in <head>.
  applyTheme(document.documentElement, loadTheme());

  function installThemeControls(doc) {
    var prefs = loadTheme();
    var controls = Array.prototype.slice.call(doc.querySelectorAll('[name="omnia-palette"], [name="omnia-mode"]'));
    function refresh() {
      applyTheme(doc.documentElement, prefs);
      controls.forEach(function (control) {
        control.checked = control.value === (control.name === "omnia-palette" ? prefs.palette : prefs.mode);
      });
      Array.prototype.forEach.call(doc.querySelectorAll(".palette-swatch"), function (swatch) {
        swatch.dataset.scheme = doc.documentElement.dataset.scheme;
      });
    }
    function change(event) {
      var input = event.currentTarget;
      var next = { palette: prefs.palette, mode: prefs.mode };
      next[input.name === "omnia-palette" ? "palette" : "mode"] = input.value;
      prefs = normalizeTheme(next);
      saveTheme(prefs);
      refresh();
    }
    controls.forEach(function (control) { control.addEventListener("change", change); });
    var m = media();
    if (m && typeof m.addEventListener === "function") m.addEventListener("change", refresh);
    window.addEventListener("focus", refresh);
    doc.addEventListener("visibilitychange", refresh);
    refresh();
    return {
      refresh: refresh,
      // LibreOffice's appearance from the exchange: "dark", "light" or null (unknown: follow the system).
      setHostScheme: function (scheme) {
        hostScheme = scheme === "dark" || scheme === "light" ? scheme : null;
        refresh();
      },
    };
  }

  window.OmniaTheme = Object.freeze({
    installThemeControls: installThemeControls,
    normalizeTheme: normalizeTheme,
    resolveScheme: resolveScheme,
  });
})();

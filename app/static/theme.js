/* Light/dark toggle. With no saved choice the page follows the OS setting; a click saves an
 * explicit choice in localStorage (a per-viewer convenience; the page works without it). */
(function () {
  "use strict";
  const root = document.documentElement;
  const media = window.matchMedia("(prefers-color-scheme: dark)");
  const current = () => root.dataset.theme || (media.matches ? "dark" : "light");

  function label() {
    const next = current() === "dark" ? "light" : "dark";
    document.querySelectorAll("[data-theme-toggle]").forEach((b) => {
      b.setAttribute("aria-label", `Switch to ${next} mode`);
      b.title = `Switch to ${next} mode`;
    });
  }

  function set(theme) {
    root.dataset.theme = theme;
    try { localStorage.setItem("theme", theme); } catch (e) { /* storage blocked: still switches */ }
    label();
    document.dispatchEvent(new CustomEvent("themechange", { detail: theme }));
  }

  document.addEventListener("click", (e) => {
    if (e.target.closest("[data-theme-toggle]")) set(current() === "dark" ? "light" : "dark");
    // the account menu (<details>) closes when you click anywhere else
    document.querySelectorAll("[data-user-menu][open]").forEach((m) => { if (!m.contains(e.target)) m.open = false; });
  });
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    document.querySelectorAll("[data-user-menu][open]").forEach((m) => { m.open = false; m.querySelector("summary").focus(); });
  });
  media.addEventListener("change", () => { if (!root.dataset.theme) { label(); document.dispatchEvent(new CustomEvent("themechange")); } });
  label();
})();

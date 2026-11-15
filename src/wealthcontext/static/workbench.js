"use strict";
// No business payload is cached in the browser. The evidence drawer shows one
// server-rendered panel, fetched with no-store and re-authorized by the server on
// every open; it is dropped when the page is hidden or the drawer closes.
const FRAGMENT = "x-workbench-fragment";
const drawer = document.getElementById("evidence-drawer");
const drawerHome = drawer ? drawer.parentElement : null;
const narrow = window.matchMedia("(max-width: 59.99rem)");
let trigger = null;

const clearEvidence = () => {
  for (const pane of document.querySelectorAll("[data-sensitive]")) pane.replaceChildren();
};

const closeDrawer = () => {
  drawer.hidden = true;
  drawer.replaceChildren();
  drawerHome.append(drawer);
  const target = trigger && trigger.isConnected ? trigger : document.getElementById("main");
  trigger = null;
  target.focus();
};

const openDrawer = async link => {
  let response;
  try {
    response = await fetch(link.href, {
      headers: {[FRAGMENT]: "drawer"}, cache: "no-store", credentials: "same-origin",
    });
  } catch {
    window.location.assign(link.href);
    return;
  }
  // A denial, an error or a redirect is a whole page: let the browser show it.
  if (!response.ok || response.redirected || response.headers.get(FRAGMENT) !== "drawer") {
    window.location.assign(link.href);
    return;
  }
  const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
  // A link inside the drawer keeps the page trigger that first opened it.
  if (!drawer.contains(link)) trigger = link;
  drawer.replaceChildren(...parsed.body.childNodes);
  // On a narrow screen the opened evidence follows its trigger in the page.
  const anchor = trigger && trigger.closest("article, section");
  if (narrow.matches && anchor) anchor.after(drawer);
  else drawerHome.append(drawer);
  drawer.hidden = false;
  const title = drawer.querySelector("#drawer-title");
  if (title) title.focus();
  if (narrow.matches) drawer.scrollIntoView({block: "nearest"});
};

if (drawer) {
  document.addEventListener("click", event => {
    // A modified click keeps its browser meaning, such as opening a new tab.
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) {
      return;
    }
    const link = event.target.closest("a[data-drawer]");
    if (link) {
      event.preventDefault();
      openDrawer(link);
      return;
    }
    // A drawer opened by a full page load closes by following its link.
    if (trigger && event.target.closest("a[data-drawer-close]")) {
      event.preventDefault();
      closeDrawer();
    }
  });
  document.addEventListener("keydown", event => {
    if (event.key !== "Escape" || drawer.hidden || document.querySelector("dialog[open]")) return;
    if (trigger) closeDrawer();
    else {
      const close = drawer.querySelector("a[data-drawer-close]");
      if (close) window.location.assign(close.href);
    }
  });
}

const dialog = document.getElementById("context-dialog");
if (dialog) {
  for (const opener of document.querySelectorAll("[data-open-context]")) {
    opener.addEventListener("click", () => dialog.showModal());
  }
  dialog.querySelector("[data-close-context]").addEventListener("click", () => dialog.close());
  // Nothing applies until the form is submitted: closing restores the shown context.
  dialog.addEventListener("close", () => dialog.querySelector("form").reset());
}

for (const form of document.querySelectorAll("form[data-busy-label]")) {
  form.addEventListener("submit", () => {
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = form.dataset.busyLabel;
  });
}
window.addEventListener("pagehide", clearEvidence);
window.addEventListener("pageshow", event => {
  if (event.persisted) window.location.reload();
});

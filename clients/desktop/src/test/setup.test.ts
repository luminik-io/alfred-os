import { describe, expect, it, vi } from "vitest";

describe("test browser isolation", () => {
  it("can leave browser-like globals dirty", () => {
    window.localStorage.setItem("alfred-dirty", "yes");
    window.history.replaceState(null, "", "/?tab=compose#ask");
    window.__TAURI_INTERNALS__ = {};
    document.title = "dirty";
    document.documentElement.dataset.theme = "dark";
    document.documentElement.classList.add("dark");
    document.documentElement.setAttribute("style", "color-scheme: dark");
    document.body.className = "dirty-body";
    document.body.style.overflow = "hidden";
    document.body.setAttribute("data-scroll-locked", "1");
    vi.stubGlobal("fetch", vi.fn());
    vi.stubGlobal("EventSource", vi.fn());
    vi.useFakeTimers();
  });

  it("starts the next test from the shared neutral surface", () => {
    expect(window.localStorage.length).toBe(0);
    expect(window.location.pathname).toBe("/");
    expect(window.location.search).toBe("");
    expect(window.location.hash).toBe("");
    expect(window.__TAURI_INTERNALS__).toBeUndefined();
    expect(document.title).toBe("");
    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(document.documentElement.getAttribute("style")).toBeNull();
    expect(document.body.className).toBe("");
    expect(document.body.getAttribute("style")).toBeNull();
    expect(document.body.hasAttribute("data-scroll-locked")).toBe(false);
    expect(vi.isMockFunction(fetch)).toBe(false);
    expect(vi.isMockFunction(window.EventSource)).toBe(false);
  });
});

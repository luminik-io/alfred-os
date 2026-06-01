import { listen } from "@tauri-apps/api/event";

// Tray menu clicks are emitted from Rust as `tray://pause-all` /
// `tray://resume-all`. This wraps the Tauri event API so the rest of the app
// stays decoupled from it, and degrades to a no-op outside the desktop shell
// (browser preview, tests) where there is no tray.
export type TrayHandlers = {
  onPauseAll: () => void;
  onResumeAll: () => void;
};

type Unlisten = () => void;

export function listenTrayEvents(handlers: TrayHandlers): Promise<Unlisten> {
  if (!isTauri()) {
    return Promise.resolve(() => {});
  }
  const offs: Unlisten[] = [];
  const ready = Promise.all([
    listen("tray://pause-all", () => handlers.onPauseAll()).then((off) => offs.push(off)),
    listen("tray://resume-all", () => handlers.onResumeAll()).then((off) => offs.push(off)),
  ]);
  return ready.then(() => () => {
    for (const off of offs) {
      off();
    }
  });
}

function isTauri(): boolean {
  return Boolean(window.__TAURI_INTERNALS__);
}

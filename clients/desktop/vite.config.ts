import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { readFileSync } from "node:fs";
import { join, resolve } from "node:path";

const env = process.env;
const host = env.TAURI_DEV_HOST;
const alfredProxyTarget = env.ALFRED_DESKTOP_PROXY_TARGET || "http://127.0.0.1:7010";
const alfredProxyOrigin = new URL(alfredProxyTarget).origin;
const SERVER_TOKEN_HEADER = "X-Alfred-Token";

function alfredProxyToken(): string | null {
  const direct = env.ALFRED_DESKTOP_PROXY_TOKEN?.trim();
  if (direct) return direct;

  const home = env.ALFRED_HOME || env.HERMES_HOME || (env.HOME ? join(env.HOME, ".alfred") : "");
  if (!home) return null;

  try {
    const token = readFileSync(join(home, "state", "server-token"), "utf8").trim();
    return token || null;
  } catch {
    return null;
  }
}

// https://vite.dev/config/
export default defineConfig(async () => ({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": resolve(__dirname, "./src"),
    },
  },

  // Vite options tailored for Tauri development and only applied in `tauri dev` or `tauri build`
  //
  // 1. prevent Vite from obscuring rust errors
  clearScreen: false,
  // 2. tauri expects a fixed port, fail if that port is not available
  server: {
    port: 1420,
    strictPort: true,
    host: host || false,
    hmr: host
      ? {
          protocol: "ws",
          host,
          port: 1421,
        }
      : undefined,
    watch: {
      // 3. tell Vite to ignore watching `src-tauri`
      ignored: ["**/src-tauri/**"],
    },
    proxy: {
      "/alfred-api": {
        target: alfredProxyTarget,
        changeOrigin: true,
        headers: {
          origin: alfredProxyOrigin,
        },
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyReq) => {
            proxyReq.setHeader("Origin", alfredProxyOrigin);
            proxyReq.setHeader("Referer", `${alfredProxyOrigin}/`);
            const token = alfredProxyToken();
            if (token) {
              proxyReq.setHeader(SERVER_TOKEN_HEADER, token);
            }
          });
        },
        rewrite: (path) => path.replace(/^\/alfred-api/, ""),
      },
    },
  },
}));

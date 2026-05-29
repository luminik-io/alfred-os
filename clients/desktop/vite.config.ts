import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// @ts-expect-error process is a nodejs global
const env = process.env;
const host = env.TAURI_DEV_HOST;
const alfredProxyTarget = env.ALFRED_DESKTOP_PROXY_TARGET || "http://127.0.0.1:7000";

// https://vite.dev/config/
export default defineConfig(async () => ({
  plugins: [react()],

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
        rewrite: (path) => path.replace(/^\/alfred-api/, ""),
      },
    },
  },
}));

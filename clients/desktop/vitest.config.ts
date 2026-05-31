import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Vitest runs the React component suite in a jsdom environment. It is kept
// separate from vite.config.ts so the Tauri dev/build pipeline stays untouched.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    environmentOptions: {
      jsdom: {
        url: "http://localhost/",
      },
    },
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
    css: false,
  },
});

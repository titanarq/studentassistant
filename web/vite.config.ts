/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";
import { backendTarget } from "./backend-target.ts";

const target = backendTarget(process.env);

export default defineConfig({
  plugins: [react()],
  build: {
    // The backend serves this directory at `/` (issue #85); it is gitignored.
    outDir: "../backend/src/studentassistant/server/static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": { target, changeOrigin: true },
      "/ws": { target, ws: true },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["src/test/setup.ts"],
  },
});

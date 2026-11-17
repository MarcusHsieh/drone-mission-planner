import { defineConfig } from "vite";

const backend = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  resolve: {
    alias: [
      // dockview-core 8.x ships its stylesheet only inside the UMD build (the
      // ESM build has none), so resolve the package to that file. Types still
      // come from the package as usual.
      { find: /^dockview-core$/, replacement: "dockview-core/dist/dockview-core.js" },
    ],
  },
  server: {
    port: 5173,
    proxy: {
      "/api": backend,
      "/ws": { target: backend.replace(/^http/, "ws"), ws: true },
    },
  },
});

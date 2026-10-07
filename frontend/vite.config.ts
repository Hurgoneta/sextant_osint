import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The UI is served by the local Python engine; `npm run dev` proxies /api to it (start it with --port 8765).
export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", sourcemap: false, chunkSizeWarningLimit: 1200, modulePreload: { polyfill: false } },
  server: { proxy: { "/api": "http://127.0.0.1:8765" } },
});

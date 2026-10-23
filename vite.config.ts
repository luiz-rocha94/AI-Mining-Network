import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  root: "apps/customer-web",
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    proxy: { "/api": process.env.AMN_API_URL || "http://127.0.0.1:8001" }
  },
  build: { outDir: "../../dist/customer-web", emptyOutDir: true }
});


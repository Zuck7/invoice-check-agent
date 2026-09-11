import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Built straight into the Python package so `invoice-audit serve` can host it
// with no separate web server and no path configuration.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: { outDir: "../invoice_audit/ui/dist", emptyOutDir: true },
  server: {
    proxy: { "/api": "http://127.0.0.1:8765" },
  },
});

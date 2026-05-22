import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  root: "src/client",
  plugins: [react()],
  build: {
    outDir: "../../dist/client",
    emptyOutDir: true
  },
  server: {
    host: "0.0.0.0",
    port: 8791
  },
  test: {
    include: ["../../tests/**/*.test.ts"],
    environment: "node"
  }
});

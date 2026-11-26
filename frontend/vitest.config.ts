import { defineConfig } from "vitest/config";
// Pure unit tests only (relative imports, no DOM): the lib helpers and the view logic next to the components.
export default defineConfig({ test: { include: ["lib/**/*.test.ts", "components/**/*.test.ts"] } });

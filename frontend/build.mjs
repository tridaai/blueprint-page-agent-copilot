/* Build the browser bundle the console serves at
 * /static/vendor/page-agent.bundle.js from the pinned page-agent
 * dependency. Run via `make frontend` (or the Docker build stage). */
import { build } from "esbuild";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));
const outfile = path.join(
  here, "..", "src", "page_agent_copilot", "static", "vendor",
  "page-agent.bundle.js");

await build({
  entryPoints: [path.join(here, "copilot-entry.js")],
  bundle: true,
  format: "iife",
  platform: "browser",
  target: ["es2022"],
  outfile,
  logLevel: "info",
});
console.log("bundled ->", outfile);

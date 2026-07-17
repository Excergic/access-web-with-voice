// build.mjs — esbuild config for Chrome MV3 extension
// Each entry point is bundled into a single self-contained file (no ES module imports).

import * as esbuild from "esbuild";

const watch = process.argv.includes("--watch");

const shared = {
  bundle: true,
  platform: "browser",
  target: ["chrome120"],
  outdir: "dist",
  // IIFE format so Chrome loads each file as a plain script (no module imports)
  format: "iife",
  sourcemap: false,
};

const entryPoints = [
  { in: "src/content.ts",    out: "content" },
  { in: "src/popup.ts",      out: "popup" },
  { in: "src/background.ts", out: "background" },
];

if (watch) {
  const ctx = await esbuild.context({ ...shared, entryPoints });
  await ctx.watch();
  console.log("Watching for changes...");
} else {
  await esbuild.build({ ...shared, entryPoints });
  console.log("Build complete.");
}

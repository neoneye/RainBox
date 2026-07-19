// Warms the local Pyodide wheel cache (runs as npm postinstall): loads every
// allowlisted package once so their wheels land in node_modules and runner.mjs
// never needs the CDN at job time.
import { loadPyodide } from "pyodide";
import { ALLOWED_PACKAGES } from "./allowed_packages.mjs";

const py = await loadPyodide();
await py.loadPackage(ALLOWED_PACKAGES);
console.log(`python_sandbox: wheel cache warmed for ${ALLOWED_PACKAGES.join(", ")}`);
process.exit(0);

// The curated set of Pyodide-shipped packages a job may import. Loaded by the
// runner only when the job's code actually imports them (startup stays fast
// otherwise), and prefetched into the local wheel cache by prefetch.mjs at npm
// install time so runtime execution needs no network.
export const ALLOWED_PACKAGES = ["numpy", "sympy", "mpmath"];

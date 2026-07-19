// Pyodide job runner: one JSON job ({"code": "..."}) on stdin, one
// "RESULT {json}" line on stdout. A fresh process per job — no state leaks.
//
// Sandboxing (what THIS file owns):
//  - Pyodide is loaded with a sanitized `jsglobals` object, so Python's `js`
//    module sees only harmless JS builtins — no `process` (host env/secrets),
//    no `fetch` (network), no `eval` (which also disables pyodide.code.run_js).
//  - In Node the real filesystem is never mounted; Python sees only Pyodide's
//    in-memory MEMFS.
//  - After load, the public escape hatches on the pyodide API object are
//    nulled: loadPackage (package installs — it would fetch from the CDN via
//    Node's own fetch, bypassing jsglobals), mountNodeFS (real-filesystem
//    mounts), FS/PATH/unpackArchive, and the private _module/_api backdoors.
//
// Resource limits (CPU / memory / wall clock) are enforced by the parent
// process (sandbox.py), which also holds the kill switch.
import { loadPyodide } from "pyodide";

const SAFE_GLOBAL_NAMES = [
  "Object", "Array", "Map", "Set", "WeakMap", "WeakSet", "Promise",
  "Error", "TypeError", "RangeError", "SyntaxError",
  "Uint8Array", "Int8Array", "Uint16Array", "Int16Array", "Uint32Array",
  "Int32Array", "Float32Array", "Float64Array", "ArrayBuffer", "DataView",
  "TextEncoder", "TextDecoder", "JSON", "Math", "Symbol", "BigInt",
  "Number", "String", "Boolean", "Date", "RegExp",
  "setTimeout", "clearTimeout", "setInterval", "clearInterval",
  "queueMicrotask", "structuredClone", "Reflect", "Proxy",
];

const NULLED_PYODIDE_PROPS = [
  "loadPackage", "loadPackagesFromImports", "mountNodeFS", "unpackArchive",
  "FS", "PATH", "_module", "_api",
];

// Runs the user code with stdout/stderr captured and the value of a trailing
// expression repr'd, and returns the outcome as a JSON string. Tracebacks are
// trimmed to the user's own frames.
const HARNESS = `
import ast, io, json, sys, traceback

def __sandbox_run():
    code = __USER_CODE__
    out = io.StringIO()
    err = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    result_repr = None
    error = None
    try:
        tree = ast.parse(code, "<job>", "exec")
        last = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = ast.Expression(tree.body[-1].value)
            del tree.body[-1]
        scope = {"__name__": "__main__"}
        exec(compile(tree, "<job>", "exec"), scope)
        if last is not None:
            value = eval(compile(last, "<job>", "eval"), scope)
            if value is not None:
                result_repr = repr(value)
    except BaseException as exc:
        tb = exc.__traceback__
        while tb is not None and tb.tb_frame.f_code.co_filename != "<job>":
            tb = tb.tb_next
        error = "".join(traceback.format_exception(type(exc), exc, tb)).strip()
    finally:
        sys.stdout, sys.stderr = old_out, old_err

    def cap(text):
        if len(text) <= 20000:
            return text
        return text[:10000] + "\\n...[truncated]...\\n" + text[-10000:]

    return json.dumps({
        "stdout": cap(out.getvalue()),
        "stderr": cap(err.getvalue()),
        "result_repr": None if result_repr is None else cap(result_repr),
        "error": None if error is None else cap(error),
    })

__sandbox_run()
`;

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
const job = JSON.parse(Buffer.concat(chunks).toString("utf8"));

const jsglobals = Object.create(null);
for (const name of SAFE_GLOBAL_NAMES) {
  if (globalThis[name] !== undefined) jsglobals[name] = globalThis[name];
}

const py = await loadPyodide({ jsglobals });
for (const name of NULLED_PYODIDE_PROPS) {
  try {
    py[name] = null;
  } catch {
    // read-only property: it stays, jsglobals still confines what it reaches
  }
}

// The parent samples baseline RSS on this marker, then starts enforcing the
// memory budget.
process.stdout.write("READY\n");

py.globals.set("__USER_CODE__", String(job.code ?? ""));
let resultJson;
try {
  resultJson = await py.runPythonAsync(HARNESS);
} catch (e) {
  resultJson = JSON.stringify({
    stdout: "", stderr: "", result_repr: null,
    error: "sandbox harness failed: " + String(e).slice(0, 2000),
  });
}
process.stdout.write("RESULT " + resultJson + "\n");
process.exit(0);

# Proposal: agent safety and operator roadmap

**Status:** Draft / proposal
**Date:** 2026-06-07
**Scope:** Safety, observability, configuration, and operator-control
improvements for Rainbox as a local single-operator agent system.

## Summary

Rainbox already has several strong safety foundations: agents are supervised
processes rather than hidden background daemons, the workspace shell has a
deterministic read-only allowlist, command execution avoids `shell=True`,
database backups are encrypted before leaving the machine, and model/provider
configuration is visible in Postgres. Those choices make the project much easier
to reason about than a tool that gives an LLM broad machine access by default.

The remaining risks are mostly about making trust boundaries explicit. MCP
configuration can currently contain static secrets, remote tools are aggregated
without a visible per-tool capability policy, there is no single health-check
command that explains whether the system is configured safely, long LLM calls
can outlive the supervisor heartbeat, and the admin surface depends on the
server remaining localhost-only. The proposed roadmap below turns these into
small, reviewable hardening steps.

## Findings

### Strong workspace shell boundary

`tools/command_policy.py` and `tools/workspace_command_runner.py` are one of the
best parts of the current design. Commands are parsed with `shlex.split`, shell
operators are rejected, only a small read-only allowlist is accepted, and
execution uses `subprocess.run(..., shell=False, ...)` with a fixed environment,
workspace-scoped `HOME`, timeout, and output cap. This is a good default for a
local agent: the model can inspect a workspace, but it cannot mutate files,
start interpreters, fetch network resources, or inherit the user's shell
environment.

### Plaintext MCP secret in configuration

*Resolved (2026-06-12):* the committed config (now `mcp.json`) holds only
non-secret demo servers; private servers with API keys live in
`<customize.dir>/mcp.json`, outside the repo, merged over the base per server
name by `mcp_config.load_mcp_servers`. The previously committed key remains in
git history and should be rotated. Original concern, kept for context:

`mcp.config.json` previously stored a remote MCP API key inline. Even in a local
single-user project, committed static secrets create avoidable risk: they can be
copied into screenshots, pushed to a fork, included in diagnostics, or reused in
unexpected environments. Secrets should come from environment variables or the
existing settings mechanism with secret redaction, and the committed file should
be an example config with placeholders only.

### MCP capability boundary is too implicit

The MCP loader can connect to stdio and remote HTTP servers, pass configured
headers, and expose the resulting tools through the function-agent path. Failed
servers are skipped, which is operationally convenient, but successful tools
become part of the agent capability set without a first-class per-server or
per-tool policy. Remote MCP servers should be treated as untrusted capability
providers: each server and tool needs explicit enablement, redacted logging, and
clear operator visibility.

### Process supervision is good but underexposed

The main supervisor has a useful control loop: it spawns child agents, tracks
PIDs, watches heartbeat timeouts, routes inbox messages, and kills hung agents.
That gives Rainbox an important containment layer. The missing piece is operator
visibility: today the most reliable view of runtime state is spread across logs,
journal rows, and source-code knowledge. A local operator should be able to see
which agents are running, what they are waiting on, and when they last produced
progress.

### Long model calls can conflict with heartbeat timeout

There is an acknowledged issue in the agent path: streaming model calls can run
longer than the supervisor heartbeat window without emitting heartbeats. A
useful reasoning call can therefore be killed as if it were hung. This is a
safety and reliability issue because it makes agent termination depend on model
latency rather than actual lack of progress.

### Admin UI assumes localhost-only operation

The Flask app binds to `127.0.0.1` by default, and the development `SECRET_KEY`
is documented as suitable only for local single-user demos. That is acceptable
for the current shape of the project, but the boundary should be enforced rather
than left as convention. If the bind host ever changes to a non-local interface,
the app should require a non-default `SECRET_KEY` and authentication or refuse
to start.

### Cron is typed, but command jobs need stronger previews

Cron jobs are not arbitrary shell snippets: the action types are explicit
(`message`, `command`, `backup`), backup runs through the encrypted backup path,
and command jobs are routed to the workspace shell. That is a good structure.
However, scheduled command jobs deserve extra UI support because they can run
without the operator being present. A job should be validated and previewed
before it can be enabled, and run history should make it obvious which command
was executed and where the result was recorded.

### Dependency hygiene is adequate but not auditable enough

The Python requirements are pinned, which is helpful for reproducibility, but
there is no hash lock, dependency audit target, or documented update process.
For a local agent system, supply-chain hygiene matters because dependencies sit
near database access, model calls, local files, and optional network services.
The project should make dependency review a repeatable maintenance task.

## Roadmap

### 1. Secure MCP configuration and secret handling

Move secrets out of committed MCP configuration. The committed
`mcp.config.json` should either become an example file or contain only
non-secret local servers and `${ENV_VAR}` placeholders. `mcp_config.py` should
resolve environment-variable placeholders for headers, redact those values in
logs and settings views, and fail closed when a required secret is missing. If a
database-backed setting is preferred, register it as a secret so the value is
never returned from normal settings listings and cannot be edited through a raw
text field that later gets rendered back to the browser.

### 2. Add a `rainbox doctor` command

Create a single diagnostic command that checks the operational safety of a
Rainbox install. It should verify database connectivity, pgvector availability,
model-provider reachability, model-group bindings, workspace-shell root and
policy, MCP configuration, backup recipient configuration, cron job validity,
server bind host, and whether the default `SECRET_KEY` is being used. The
output should have both a human terminal format and a JSON format so it can be
used from CI or a future settings page. Each check should return `pass`,
`warn`, or `fail` with a concrete remediation message.

### 3. Build a capability registry

Add a code-side registry that describes every capability an agent can reach:
workspace shell commands, cron actions, backup push, MCP servers, MCP tools, and
future mutating tools. Each entry should declare whether it reads local state,
writes local state, uses the network, touches secrets, requires confirmation,
and is enabled by default. This registry should drive UI badges, diagnostics,
and tests, so safety documentation is generated from the same facts the app
uses. The goal is not to block all power, but to make every capability visible
and intentionally enabled.

### 4. Add explicit side-effect policy and approvals

Keep the workspace shell read-only by default, and require explicit policy for
anything that mutates files, pushes data, sends network requests, or calls an
external tool with side effects. Cron command jobs, backup git pushes, and MCP
tools are the first places to apply this. The implementation can start small:
add a `requires_confirmation` flag, an `enabled` flag, and an audit row for each
approved side-effect path. Later, if write-capable workspace tools are added,
they should be introduced one command at a time with validators and tests.

### 5. Add an agent runtime dashboard

Expose the supervisor's runtime knowledge in the web UI. A dashboard should show
running agents, PID, role, current inbox or journal item, model/provider, start
time, heartbeat age, last status, last error, and a bounded log tail. It should
also provide explicit kill or retry controls where the supervisor already has
the underlying capability. This would make Rainbox easier to operate without
watching terminal output and would make hung agents, slow model calls, and
misconfigured providers much easier to diagnose.

### 6. Emit heartbeats during long-running LLM work

Agent code should emit progress heartbeats while waiting on streaming or
structured model calls. These heartbeats should record status such as
`waiting_for_model`, provider name, model group, elapsed time, and token/output
progress where available, without storing private chain-of-thought. That lets
the supervisor distinguish a live long-running call from a dead process. It also
makes it safer to reduce model retry loops in the agent itself and let
model-group fallback handle provider-level retries.

### 7. Harden the MCP execution boundary

Treat every MCP server as untrusted until explicitly enabled. Add per-server
and per-tool allowlists, fixed timeouts, output caps, URL allowlists for remote
servers, exact command definitions for local stdio servers, and redacted
argument/result logging. Remote MCP tools should remain disabled until their
required secret configuration is present and the operator has enabled the
server. Failed or disabled servers should produce clear diagnostics rather than
silently disappearing from the operator's mental model.

### 8. Improve cron safety and auditability

Before a `command` cron job can be saved or enabled, run it through the same
workspace-shell validator used at execution time and show the parsed command,
working directory, and expected target agent. Run history should link each
`CronRun` to the message or journal row it created, and the cron UI should show
next fire time, last result, enabled state, and whether the job has passed a
manual test run. This preserves the useful typed cron design while reducing the
chance that an old scheduled command surprises the operator later.

### 9. Add localhost and admin guardrails

Document and enforce the current assumption that Rainbox is a local
single-operator app. Startup should warn or refuse when the app is bound to a
non-local interface while using the default `SECRET_KEY` or no authentication.
The admin UI should visibly indicate that it is local-only, and any future
network-exposed mode should require an explicit configuration switch,
non-default secret, and authentication layer. This keeps the current lightweight
admin surface while preventing accidental exposure.

### 10. Make the feedback and eval loop easier to operate

Rainbox already has the right ingredients for iterative improvement: feedback,
eval cases, model groups, and retrieval signals. The next step is to make those
flows visible and repeatable. Add UI actions to promote feedback into eval
cases, run a candidate model or prompt against a selected suite, compare results
against the current baseline, and inspect retrieval downvote rollups. This
keeps improvement evidence-based instead of relying on anecdotal prompt edits.

### 11. Improve setup and onboarding

Add a first-run checklist or bootstrap guide that aligns with the doctor
command. It should cover database creation, pgvector, provider base URLs, model
group setup, workspace-shell root, backup recipient setup, MCP configuration,
and cron defaults. A sample `.env` is useful, but it should contain only
placeholders and local defaults. The goal is for a new operator to know exactly
which parts are configured, which are optional, and which are unsafe to skip.

### 12. Add dependency and supply-chain maintenance

Add a documented dependency audit command, such as `pip-audit`, and decide
whether Rainbox should use a hash-locked requirements file for deployed runs.
The project should also document how dependency updates are made, how audit
findings are triaged, and which runtime dependency-fetch patterns are
disallowed. The current pinned requirements are a good start; the missing piece
is repeatable verification and a clear maintenance habit.

## Suggested first PRs

1. Replace static MCP secrets with placeholders, add environment-variable
   interpolation for MCP headers, redact secrets in diagnostics, and add a test
   that committed MCP config contains no literal API keys.
2. Add a minimal `rainbox doctor` command with checks for database access,
   workspace-shell policy, MCP config, backup recipient configuration, bind host,
   and `SECRET_KEY`.
3. Introduce a capability registry for built-in tools, cron actions, backup
   push, and configured MCP servers, then render those capabilities in a simple
   admin or settings view.
4. Add model-call heartbeat emission so the supervisor no longer kills live
   long-running model calls solely because no heartbeat was sent during
   streaming.

## Acceptance criteria

- No committed configuration file contains reusable secrets.
- A fresh local install can run one command that reports the safety state of the
  database, model providers, workspace shell, MCP, backups, cron, and admin
  binding.
- Every network, secret, write, or push capability is visible in a registry and
  has an explicit enabled/disabled state.
- Long-running model calls remain observable to the supervisor while they are in
  progress.
- Cron command jobs are validated before enablement and have linked execution
  history.

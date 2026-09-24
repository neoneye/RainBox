"""Diary memory: read-only search over the operator's own diary files.

Design and contracts: notes/proposals/2026-09-21-diary-memory-representation-proposals.md.

Modules:
- `config`   — manifest validation, parser configuration and its fingerprint.
- `parsing`  — `parse_file`, a pure function from bytes to entries/passages.
"""

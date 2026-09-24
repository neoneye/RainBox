# Diary fixture

Synthetic diary files for the diary parser, sync and retrieval tests. All
content is invented (Terminator-universe names, example.org hosts); none of it
comes from a real diary. Tests pin byte ranges into these files, so edit them
only together with the tests that reference them.

- `current/` — the `timed` dialect: `YYYYMMDD` date lines, `HHhMM` entries.
- `daily/` — the `daily` dialect: one file per day, `HHMM` entries, pasted
  terminal sessions.
- `archive/` — the `changelog` dialect: newest day first, month names in
  Danish/German/English, one author token per header.

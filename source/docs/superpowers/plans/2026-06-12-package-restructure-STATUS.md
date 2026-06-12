# Package restructure — execution status

**Completed:** 2026-06-12. All 11 plan tasks executed and reviewed on branch
`restructure-packages`; final whole-branch review verdict: ready to merge.

Final verification: pytest at exact baseline (2 pre-existing failures in
`agents/test_query_filter_router_memory_ops.py`, 993 passed, 10 skipped),
pyright at baseline (740 pre-existing errors), live boot + demo pipeline via
`python -m agents` + clean SIGINT shutdown on `rainbox_claude`, backup CLI via
`python -m backup.dump`.

Operator handoff: `python backup_db.py …` → `python -m backup.dump …`; any
external scripts/cron 'command' rows referencing old script paths need the new
`-m` forms. Pre-existing (untouched): the two failing query_filter_router
tests, and the kokoro/whisper `test_server.py` pytest basename collision.

# Archived recovery scripts

These were one-off migration, reconciliation, diagnostic, and Google Tasks
repair tools. They are not part of the supported application workflow and have
no Make targets or README instructions.

They remain only as historical reference. Do not run them without deliberately
reviewing their code and the current data model first. In particular,
`restore_task.py` can write to Google Tasks when invoked with `--apply`.

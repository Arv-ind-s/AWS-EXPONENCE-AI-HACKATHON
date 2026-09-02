# Contributing to Covenant Radar

1. **Read** the block and only the paths its `Read first` names, plus `plan.md §6` for the contracts it lists.
2. **Branch** `task/t-0NN-<slug>` from the current main line; record the base commit.
3. **Build** inside `Files owned`. Write the named tests as you go.
4. **Prove** with `python -m radarctl gate --fast`, then each `Run` command with its stated expected result.
5. **Hand over** with the task id, the commands run, their output, and anything the block did not anticipate.
6. **Wait** for human review and merge. Never merge your own work.
7. **Record** in `MERGE_LOG.md`: task id, commit, revert command, plan days, actual days.

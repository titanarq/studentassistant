# Architecture decision records

One binding decision per file, numbered. Agents read the ones their issue or module doc links;
only the human edits this directory. A proposal to change a decision goes in a PR description
or a new issue.

| ADR | Decision |
|---|---|
| [0001](0001-thin-phone-smart-pc.md) | Thin Android capture client, all intelligence on the Ubuntu backend; audio stream + on-demand stills, no video stream |
| [0002](0002-the-vault-is-a-git-repository-and-the-source-of-truth.md) | All content lives in a separate private git repo (the vault) synced to GitHub; local DB is a derived cache |
| [0003](0003-sessions-are-event-sourced.md) | A study session is an append-only event log; observer state is a fold of it |
| [0004](0004-claude-is-reached-only-through-the-llm-module.md) | Claude is reached only through `studentassistant.llm`; models per role in config |
| [0005](0005-every-paragraph-carries-provenance.md) | Master notes are Markdown whose every paragraph cites its sources; AI additions are marked |
| [0006](0006-voice-commands-are-deterministic.md) | Voice commands are a deterministic grammar over the transcript, never an LLM decision |
| [0007](0007-core-technology-choices.md) | Toolchains and libraries |

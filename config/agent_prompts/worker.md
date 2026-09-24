BUILDING AND TESTING (studentassistant monorepo)
- Run tests only through `bash __TEST_COMMAND__` (all suites) or narrow it:
  `bash __TEST_COMMAND__ backend -k vault`, `bash __TEST_COMMAND__ web`,
  `bash __TEST_COMMAND__ android :app:testDebugUnitTest`. Read `.cache/test-<suite>-last.log`
  instead of re-running.
- Backend: `uv` inside `backend/` (`uv add`, `uv run`); commit `uv.lock` changes. Never call the
  real Claude API, never download Whisper models, never touch `~/.config/studentassistant` or a
  real vault: use `FakeClaude`, `FakeTranscriber` and the `tmp_vault` fixture.
  `@pytest.mark.integration` tests are written but never run by you.
- Web: `npm` inside `web/`; commit `package-lock.json` changes.
- Android: `ANDROID_HOME` is exported. Do not install SDK packages or add dependencies outside
  `android/gradle/libs.versions.toml`; do not change the Gradle wrapper version once it exists.
  Before any Gradle run, check no other Gradle build runs for your worktree:
  `ps -eo pid,cmd | grep [G]radleWrapperMain`.
- Student-facing text (UI strings, prompts' output language, generated notes) is Spanish; code,
  identifiers, comments and commits are English.

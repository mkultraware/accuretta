# Contributing to Accuretta

Pull requests are welcome. A few rules keep the code healthy.

## Before you start

- Open an issue first for anything bigger than a small fix, so we can agree on the approach.
- Contributions are accepted under the [Contributor License Agreement](CLA.md). Tick the box
  in the pull request description to agree.

## Rules for changes

1. **One change per pull request.** One purpose: a fix, a feature or a refactor. Never mix a
   behaviour change with renaming, moving or reformatting code.
2. **Tests come with behaviour changes.** Add or update a test that fails without your change
   and passes with it.
3. **Run the checks before you push:**

   ```
   python tests/check.py
   ```

   It runs JavaScript and CSS syntax checks (skipped when Node.js isn't installed) and the
   test suite. Every test runs in a throwaway data folder, so your own chats and settings are
   never touched.
4. **Line endings:** `bridge.py`, `app.js`, `README.md` and most CSS files use Windows line
   endings (CRLF). Keep them that way. Many editors keep line endings as they are; check that
   yours does.
5. **No new settings for users.** A fix should work on every setup by detecting what it needs
   at runtime, not by adding an option.
6. **No personal data.** Never commit API keys, tokens, real chat logs, personal paths or
   anything from your `data/` folder.
7. **Keep it general.** Accuretta runs on many GPUs, models and Windows languages. Don't
   assume yours.

## Writing style

Comments, commit messages and UI text use plain English: short sentences, everyday words.

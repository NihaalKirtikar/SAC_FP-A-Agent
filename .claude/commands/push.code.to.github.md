---
description: Stage, commit & push your work to GitHub
---

Push the user's local changes to the shared GitHub repo:

1. Run `git status` and `git diff` to see exactly what changed, staged and
   unstaged.
2. Before staging anything, scan the changed/untracked file list for
   anything that could be a secret — `.env`, credentials, API keys, tokens —
   even if the filename looks innocuous. `.env` itself is gitignored, but a
   NEW file with secrets in it wouldn't be automatically caught. If anything
   looks sensitive, stop and flag it instead of staging it.
3. Stage the specific files that make up this change (not a blanket
   `git add -A`/`git add .`) — list them explicitly.
4. Draft a concise commit message focused on *why*, matching this repo's
   existing commit style (`git log` for examples). Multi-part changes get a
   short bullet body, like the existing history already does.
5. Show the user the staged diff summary and the drafted message, and get an
   explicit go-ahead before committing — this pushes to a repo other
   contributors pull from, so a bad push affects everyone, not just this
   checkout.
6. Commit, then `git push`. If the push is rejected because the remote has
   commits this branch doesn't have, STOP and tell the user to pull first
   (see `/pull.code.from.github`) — never force-push.

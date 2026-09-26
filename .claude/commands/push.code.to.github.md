---
description: Stage, commit & open a pull request for your work (no direct pushes to main)
---

This repo uses a PR-based workflow — nobody commits directly on `main`,
including this command. Propose the user's local changes as a pull request:

1. Run `git status` and `git diff` to see exactly what changed, staged and
   unstaged.
2. Before staging anything, scan the changed/untracked file list for
   anything that could be a secret — `.env`, credentials, API keys, tokens —
   even if the filename looks innocuous. `.env` itself is gitignored, but a
   NEW file with secrets in it wouldn't be automatically caught. If anything
   looks sensitive, stop and flag it instead of staging it.
3. If currently ON `main`, create a new branch for this change first
   (`git checkout -b <short-descriptive-name>`) — never commit this directly
   on `main`.
4. Stage the specific files that make up this change (not a blanket
   `git add -A`/`git add .`) — list them explicitly.
5. Draft a concise commit message focused on *why*, matching this repo's
   existing commit style (`git log` for examples). Multi-part changes get a
   short bullet body, like the existing history already does.
6. Show the user the staged diff summary and the drafted message, and get an
   explicit go-ahead before committing — this proposes a change every
   contributor will review, so get it right before it's public.
7. Commit, then `git push -u origin <branch-name>`.
8. Open the pull request targeting `main`:
   - If the `gh` CLI is available (`gh auth status` succeeds), run
     `gh pr create --base main --head <branch-name> --title "..." --body "..."`
     using the same commit message content for the title/body.
   - Otherwise, give the user the direct compare link so they can open it in
     their browser: `https://github.com/<owner>/<repo>/compare/main...<branch-name>?expand=1`
     (fill in `<owner>/<repo>` from `git remote get-url origin`).
9. Never merge the PR — that's for the repo owner/a reviewer to do, not this
   command.

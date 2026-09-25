---
description: Pull all latest changes from other contributors
---

Bring this local checkout up to date with the shared GitHub repo:

1. Run `git status`. If there are uncommitted local changes, STOP and tell the
   user — do not discard, stash, or commit their work for them. Let them
   decide whether to commit or stash first, then re-run this command.
2. Run `git pull origin <current-branch>` (use the branch `git branch --show-current`
   reports, not a hardcoded name).
3. If the pull reports merge conflicts, STOP and list the conflicting files
   plainly. Do not attempt to auto-resolve them.
4. If `requirements.txt` changed in the pull, tell the user to reinstall:
   `pip install -r requirements.txt` inside their `.venv`.
5. If `.env.example` changed in the pull, diff it against the user's own
   `.env` and point out any new keys they're missing — never read or print
   the user's actual `.env` values.
6. Summarize what came in (`git log --oneline <old-head>..<new-head>`) so the
   user knows what's new, in plain language grouped by theme if there are
   many commits — not a raw commit dump.

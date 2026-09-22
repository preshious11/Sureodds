# Project rules

- Never add Claude, Claude Code, or any Anthropic-related account/bot as a collaborator on this GitHub repository (no invites via `gh api`, `gh repo`, repo settings, CODEOWNERS, etc.).
- Do not add a `Co-Authored-By: Claude ... <noreply@anthropic.com>` (or any Anthropic-attributed) trailer to commit messages, and do not add a "Generated with Claude Code" line to PR descriptions. GitHub renders `Co-Authored-By` trailers as a co-author on the commit and in the contributor graph, which makes it look like Claude is a repo collaborator/contributor — it isn't, and it must not appear that way. Commits and PRs should be authored/attributed to the user only.

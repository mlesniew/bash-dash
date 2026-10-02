# Computer Handoff Session

## Goal
Let a participant scan the booth QR on a phone and start the resulting game on a computer with a short code.

## Changes
- Added permanent `GameTicket` records with unique five-character codes from an unambiguous alphabet.
- Made Start atomically consume one ticket and link it to exactly one `GameSession`.
- Changed `/` without a QR token into the computer code-entry screen while retaining manual six-digit gate codes.
- Kept mobile play available from the QR landing page.
- Added read-only phone polling for games started on a computer and summary access after completion.
- Registered tickets for read-only staff inspection and added migration `0009_gameticket`.
- Updated README and PRD documentation for the handoff flow.

## Verification
- `manage.py test`: 270 passed, 4 skipped.
- `manage.py check`: passed.
- `manage.py makemigrations --check --dry-run`: no changes detected.
- `node --check game/static/game/ticket.js`: passed.
- `git diff --check`: passed.
- Browser smoke test: mobile QR landing rendered at 375 px, lowercase code redemption started the desktop game, and phone polling switched to read-only "Game in progress".

## Notes
- Game ticket codes never expire and are never reused.
- The accepted code alphabet is `ACDEFHJKMNPQRTUVWXY3479`.
- A used ticket cannot recover a lost computer browser session; it only lets the original phone observe status and the final summary.

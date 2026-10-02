# Project Progress

## Current Session
- Goal: Hide the computer handoff recommendation when the booth code is entered manually.
- Status: completed

## Recent Changes
- 2026-10-02: Manual 6-digit booth-code entry now opens the local Start form without suggesting a computer handoff.
- 2026-10-02: Shortened the mobile fallback separator to "Or continue here".
- 2026-10-02: Reworked the QR landing copy to explain why a computer improves the chance of a high score and clearly separated the mobile option.
- 2026-10-02: Added permanent one-time game tickets for moving play from a scanned phone to a computer.
- 2026-10-02: Added read-only mobile status polling and post-game summary access for computer-started games.
- 2026-10-02: Added migration and service/view coverage; all 270 tests pass (4 Docker tests skipped).
- 2026-10-01: Added Up Arrow recall for the last command executed in the gameplay answer box.
- 2026-10-01: Added a persisted, staff-configurable correct-answer time bonus with a 15-second default.
- 2026-10-01: Added the live bonus to the onboarding instructions and documented staff configuration.
- 2026-10-01: Added service, view, staff authorization, and migration coverage; all 252 tests pass.

## Next Steps
- [ ] Apply migrations through `0009_gameticket` during deployment.

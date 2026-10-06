# Manual Gate Code Session

## Goal
Avoid showing the computer handoff recommendation when a participant already entered the booth code on a computer.

## Changes
- Classified a manually entered six-digit booth code as a local code-entry flow rather than a QR scan.
- Kept the recommendation visible for direct `?t=` QR links.
- Updated view coverage for both entry paths.

## Verification
- Focused gate and handoff tests: 17 passed.
- Full `manage.py test`: 270 passed, 4 skipped.
- `manage.py check`: passed.
- `git diff --check`: passed.

## Notes
- No device detection was added; behavior follows the user's entry path.
- A laptop that directly scans and opens the QR URL still sees the recommendation by design.

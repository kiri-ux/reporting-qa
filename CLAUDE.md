
## Do not explain the fix on the screen

Every finding, panel and banner states the fact and stops. No sentence saying
why a number is what it is, what changed, or what a check has already decided -
that reasoning goes in the code comment, where the next person changing it
needs it, and nowhere the user reads.

This has been asked for repeatedly. A wording problem is never answered by
adding wording.

## Bump the build and say which it is

Every change that merges bumps `BUILD` in `app/version.py` (date, then the
next number: `2026.10.01-263` -> `2026.10.01-264`). Every reply about a merged
change names the build it went out in. The footer shows the build, and it is
how a deploy is confirmed live.

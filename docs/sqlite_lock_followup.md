# SQLite lock follow-up

The existing `control/command_bus.py` catches `sqlite3.OperationalError` and
skips a polling tick when the command database is locked.  The scanner's
`_connect_db` uses a timeout but does not set an explicit busy timeout, so a
concurrent writer can still surface as a lost scan/persistence attempt.

This offline scoring package deliberately does not touch persistence.  A
future persistence change should add a focused contention test with two
connections, assert retry/visibility semantics, and keep shadow scoring
usable when the database is unavailable.

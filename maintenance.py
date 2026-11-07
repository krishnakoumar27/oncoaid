"""Coordinate permanent resets with background jobs and message delivery."""
from contextlib import contextmanager

LOCK = 783315


def transaction_guard(db, exclusive=False):
    if exclusive:
        db.execute("SET LOCAL lock_timeout='30s'")
    function = "pg_advisory_xact_lock" if exclusive else "pg_advisory_xact_lock_shared"
    db.execute(f"SELECT {function}(%s)", (LOCK,))


@contextmanager
def delivery_guard(db):
    """Hold across commits and network delivery; reset waits for in-flight sends."""
    db.execute("SELECT pg_advisory_lock_shared(%s)", (LOCK,))
    db.commit()
    try:
        yield
        db.commit()
    finally:
        # Release even when a provider/SQL error left the transaction aborted.
        db.rollback()
        db.execute("SELECT pg_advisory_unlock_shared(%s)", (LOCK,))
        db.commit()

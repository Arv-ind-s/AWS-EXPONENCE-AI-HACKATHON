"""add the explicit account-activity signal family

The product treats daily account activity as a first-class early-warning
input, distinct from payment delays and treasury outflows.  Existing rows are
unchanged; only the closed family checks on the two signal tables widen.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0005_account_activity_signal"
down_revision: str | None = "0004_notification_read_state"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_FAMILIES = (
    "account_activity",
    "payment",
    "utilisation",
    "treasury",
    "concentration",
    "industry",
    "news",
)


def _check_sql() -> str:
    return "family IN (" + ", ".join(repr(value) for value in _FAMILIES) + ")"


def upgrade() -> None:
    # SQLite cannot alter a CHECK constraint in place, so batch recreation is
    # required there.  PostgreSQL accepts the same operation and preserves all
    # existing columns, indexes, foreign keys, and rows.
    for table in ("signal_event", "evidence_item"):
        with op.batch_alter_table(table, schema=None, recreate="always") as batch_op:
            # The application's naming convention expands ``family_valid``
            # to ``ck_<table>_family_valid`` during batch recreation.  Passing
            # the logical name avoids Alembic applying that convention twice.
            batch_op.drop_constraint("family_valid", type_="check")
            batch_op.create_check_constraint("family_valid", _check_sql())


def downgrade() -> None:
    legacy = ("payment", "utilisation", "treasury", "concentration", "industry", "news")
    sql = "family IN (" + ", ".join(repr(value) for value in legacy) + ")"
    for table in ("signal_event", "evidence_item"):
        with op.batch_alter_table(table, schema=None, recreate="always") as batch_op:
            batch_op.drop_constraint("family_valid", type_="check")
            batch_op.create_check_constraint("family_valid", sql)

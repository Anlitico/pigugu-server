"""merge heads 56cca7669edf and 849d031a033c

Revision ID: c3d4e5f6a7b8
Revises: 56cca7669edf, 849d031a033c
Create Date: 2026-06-16 17:50:00.000000

"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = ('56cca7669edf', '849d031a033c')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

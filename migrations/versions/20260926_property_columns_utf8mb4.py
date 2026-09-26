"""Convert remaining property text columns to utf8mb4."""

from alembic import op
import sqlalchemy as sa

revision = '20260926_prop_cols_utf8mb4'
down_revision = '20260926_prop_desc_utf8mb4'
branch_labels = None
depends_on = None


_UPGRADE_SQL = '''
    ALTER TABLE property
        MODIFY COLUMN prop_title VARCHAR(100)
            CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL,
        MODIFY COLUMN prop_type VARCHAR(50)
            CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL,
        MODIFY COLUMN listing_type VARCHAR(20)
            CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL,
        MODIFY COLUMN prop_location VARCHAR(250)
            CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL,
        MODIFY COLUMN prop_state VARCHAR(45)
            CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL,
        MODIFY COLUMN prop_address VARCHAR(50)
            CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL,
        MODIFY COLUMN prop_lga VARCHAR(120)
            CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL,
        MODIFY COLUMN prop_area_unit VARCHAR(20)
            CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NULL DEFAULT 'sqm',
        MODIFY COLUMN prop_status VARCHAR(20)
            CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci NOT NULL DEFAULT 'available'
'''

_DOWNGRADE_SQL = '''
    ALTER TABLE property
        MODIFY COLUMN prop_title VARCHAR(100)
            CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci NOT NULL,
        MODIFY COLUMN prop_type VARCHAR(50)
            CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci NOT NULL,
        MODIFY COLUMN listing_type VARCHAR(20)
            CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci NULL,
        MODIFY COLUMN prop_location VARCHAR(250)
            CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci NOT NULL,
        MODIFY COLUMN prop_state VARCHAR(45)
            CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci NOT NULL,
        MODIFY COLUMN prop_address VARCHAR(50)
            CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci NOT NULL,
        MODIFY COLUMN prop_lga VARCHAR(120)
            CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci NULL,
        MODIFY COLUMN prop_area_unit VARCHAR(20)
            CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci NULL DEFAULT 'sqm',
        MODIFY COLUMN prop_status VARCHAR(20)
            CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci NOT NULL DEFAULT 'available'
'''


def _execute_mysql(sql):
    bind = op.get_bind()
    if bind.dialect.name not in {'mysql', 'mariadb'}:
        raise RuntimeError('This migration requires a MySQL-compatible database.')
    bind.execute(sa.text(sql))


def upgrade():
    _execute_mysql(_UPGRADE_SQL)


def downgrade():
    _execute_mysql(_DOWNGRADE_SQL)

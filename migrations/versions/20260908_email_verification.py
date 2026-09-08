"""Add secure email verification fields to users."""

from alembic import op
import sqlalchemy as sa

revision = '20260908_email_verification'
down_revision = ('20260709_admin_account_schema', '20260709_create_contact_messages')
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column['name'] for column in inspector.get_columns('users')}

    if 'email_verification_token_hash' not in columns:
        op.add_column('users', sa.Column('email_verification_token_hash', sa.String(length=64), nullable=True))
        op.create_index('ix_users_email_verification_token_hash', 'users', ['email_verification_token_hash'], unique=True)
    if 'email_verification_expires_at' not in columns:
        op.add_column('users', sa.Column('email_verification_expires_at', sa.DateTime(), nullable=True))

    # Existing accounts predate email verification; preserve their access.
    bind.execute(sa.text("UPDATE users SET user_verified = 1 WHERE user_verified = 0 OR user_verified IS NULL"))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column['name'] for column in inspector.get_columns('users')}
    if 'email_verification_token_hash' in columns:
        try:
            op.drop_index('ix_users_email_verification_token_hash', table_name='users')
        except Exception:
            pass
        op.drop_column('users', 'email_verification_token_hash')
    if 'email_verification_expires_at' in columns:
        op.drop_column('users', 'email_verification_expires_at')

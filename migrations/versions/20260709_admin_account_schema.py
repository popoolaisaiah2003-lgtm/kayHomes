"""Add admin account management columns to the admin table.

This migration only alters the existing admin table and does not touch any
other tables.
"""

from __future__ import annotations

import os
import sys

from sqlalchemy import inspect, text

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, '..', '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from pkg import app
from pkg.models import db

revision = '20260709_admin_account_schema'
down_revision = None
branch_labels = None
depends_on = None


def _column_names():
    inspector = inspect(db.engine)
    if not inspector.has_table('admin'):
        return set()
    return {column['name'] for column in inspector.get_columns('admin')}


def upgrade():
    with app.app_context():
        inspector = inspect(db.engine)
        if not inspector.has_table('admin'):
            return

        columns = _column_names()
        statements = []

        if 'first_name' not in columns:
            statements.append("ALTER TABLE admin ADD COLUMN first_name VARCHAR(100) NOT NULL DEFAULT ''")
        if 'last_name' not in columns:
            statements.append("ALTER TABLE admin ADD COLUMN last_name VARCHAR(100) NOT NULL DEFAULT ''")
        if 'username' not in columns:
            statements.append('ALTER TABLE admin ADD COLUMN username VARCHAR(100) NOT NULL')
        if 'email' not in columns:
            statements.append('ALTER TABLE admin ADD COLUMN email VARCHAR(120) NOT NULL')
        if 'phone' not in columns:
            statements.append('ALTER TABLE admin ADD COLUMN phone VARCHAR(20) NULL')
        if 'password' not in columns:
            statements.append('ALTER TABLE admin ADD COLUMN password VARCHAR(255) NOT NULL')
        if 'profile_image' not in columns:
            statements.append('ALTER TABLE admin ADD COLUMN profile_image VARCHAR(255) NULL')
        if 'role' not in columns:
            statements.append("ALTER TABLE admin ADD COLUMN role VARCHAR(50) NOT NULL DEFAULT 'admin'")
        if 'status' not in columns:
            statements.append("ALTER TABLE admin ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'Active'")
        if 'created_at' not in columns:
            statements.append('ALTER TABLE admin ADD COLUMN created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP')
        if 'updated_at' not in columns:
            statements.append('ALTER TABLE admin ADD COLUMN updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP')

        for statement in statements:
            db.session.execute(text(statement))
        if statements:
            db.session.commit()
            columns = _column_names()

        legacy_username_columns = ['adm_username', 'adm_user_name', 'admin_username', 'username', 'user_name']
        legacy_password_columns = ['adm_pwd', 'admin_pwd', 'admin_password', 'password', 'pwd']

        username_source = next((col for col in legacy_username_columns if col in columns and col != 'username'), None)
        password_source = next((col for col in legacy_password_columns if col in columns and col != 'password'), None)

        if username_source:
            db.session.execute(
                text(
                    f"UPDATE admin SET username = {username_source} "
                    f"WHERE (username IS NULL OR username = '') AND {username_source} IS NOT NULL"
                )
            )
        if password_source:
            db.session.execute(
                text(
                    f"UPDATE admin SET password = {password_source} "
                    f"WHERE (password IS NULL OR password = '') AND {password_source} IS NOT NULL"
                )
            )
        db.session.execute(text("UPDATE admin SET role = 'admin' WHERE role IS NULL OR role = ''"))
        db.session.execute(text("UPDATE admin SET status = 'Active' WHERE status IS NULL OR status = ''"))
        db.session.commit()

        indexes = {index['name'] for index in inspector.get_indexes('admin')}
        if 'uq_admin_username' not in indexes:
            db.session.execute(text('ALTER TABLE admin ADD UNIQUE KEY uq_admin_username (username)'))
        if 'uq_admin_email' not in indexes:
            db.session.execute(text('ALTER TABLE admin ADD UNIQUE KEY uq_admin_email (email)'))
        db.session.commit()


def downgrade():
    with app.app_context():
        inspector = inspect(db.engine)
        if not inspector.has_table('admin'):
            return

        columns = _column_names()
        for column_name in ['updated_at', 'created_at', 'status', 'role', 'profile_image', 'password', 'phone', 'email', 'username', 'last_name', 'first_name']:
            if column_name in columns:
                db.session.execute(text(f'ALTER TABLE admin DROP COLUMN {column_name}'))
        db.session.commit()


if __name__ == '__main__':
    upgrade()

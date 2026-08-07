"""Create contact_messages table.

This migration is intentionally narrow: it only creates the new contact_messages
 table and does not modify any existing tables.
"""

from sqlalchemy import inspect, text
import os
import sys

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(CURRENT_DIR, '..', '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from pkg import app
from pkg.models import db

revision = '20260709_create_contact_messages'
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    with app.app_context():
        inspector = inspect(db.engine)
        if inspector.has_table('contact_messages'):
            return

        db.session.execute(text('''
            CREATE TABLE IF NOT EXISTS contact_messages (
                message_id INT AUTO_INCREMENT PRIMARY KEY,
                name VARCHAR(100) NOT NULL,
                email VARCHAR(120) NOT NULL,
                phone VARCHAR(20) NULL,
                subject VARCHAR(150) NULL,
                message TEXT NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'Unread',
                created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
            )
        '''))
        db.session.commit()



def downgrade():
    with app.app_context():
        inspector = inspect(db.engine)
        if not inspector.has_table('contact_messages'):
            return

        db.session.execute(text('DROP TABLE IF EXISTS contact_messages'))
        db.session.commit()


if __name__ == '__main__':
    upgrade()

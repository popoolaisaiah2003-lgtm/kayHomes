from flask import Flask
from pkg.models import db
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
import os

app = Flask(__name__, instance_relative_config=True)

# Load instance configuration before initializing extensions.
os.makedirs(app.instance_path, exist_ok=True)
app.config.from_pyfile('config.py', silent=True)
app.config['SECRET_KEY'] = 'securedkey'
if not app.config.get('SQLALCHEMY_DATABASE_URI'):
    app.config['SQLALCHEMY_DATABASE_URI'] = 'mysql+pymysql://root:@localhost/kayhomes'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

# Uploads
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db.init_app(app)


def ensure_admin_schema_compatibility():
    """Ensure the admin table matches the current account-management model."""
    with app.app_context():
        try:
            inspector = inspect(db.engine)
            if not inspector.has_table('admin'):
                return

            columns = {col['name'] for col in inspector.get_columns('admin')}
            schema_changed = False

            required_columns = {
                'first_name': "ALTER TABLE admin ADD COLUMN first_name VARCHAR(100) NOT NULL DEFAULT ''",
                'last_name': "ALTER TABLE admin ADD COLUMN last_name VARCHAR(100) NOT NULL DEFAULT ''",
                'username': 'ALTER TABLE admin ADD COLUMN username VARCHAR(100) NOT NULL',
                'email': 'ALTER TABLE admin ADD COLUMN email VARCHAR(120) NOT NULL',
                'phone': 'ALTER TABLE admin ADD COLUMN phone VARCHAR(20) NULL',
                'password': 'ALTER TABLE admin ADD COLUMN password VARCHAR(255) NOT NULL',
                'profile_image': 'ALTER TABLE admin ADD COLUMN profile_image VARCHAR(255) NULL',
                'role': "ALTER TABLE admin ADD COLUMN role VARCHAR(50) NOT NULL DEFAULT 'admin'",
                'status': "ALTER TABLE admin ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'Active'",
                'created_at': 'ALTER TABLE admin ADD COLUMN created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP',
                'updated_at': 'ALTER TABLE admin ADD COLUMN updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP',
            }

            for column_name, alter_statement in required_columns.items():
                if column_name not in columns:
                    db.session.execute(text(alter_statement))
                    schema_changed = True

            if schema_changed:
                db.session.commit()
                inspector = inspect(db.engine)
                columns = {col['name'] for col in inspector.get_columns('admin')}

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

            if 'role' in columns:
                db.session.execute(
                    text("UPDATE admin SET role = 'admin' WHERE role IS NULL OR role = ''")
                )

            if 'status' in columns:
                db.session.execute(
                    text("UPDATE admin SET status = 'Active' WHERE status IS NULL OR status = ''")
                )

            if username_source or password_source or 'role' in columns or 'status' in columns:
                db.session.commit()
        except (OperationalError, SQLAlchemyError) as exc:
            db.session.rollback()
            print(f'Skipping admin schema compatibility due to database error: {exc}')


def ensure_category_schema_compatibility():
    """Ensure categories are database-driven and linked to properties."""
    with app.app_context():
        try:
            inspector = inspect(db.engine)
            dialect_name = db.engine.dialect.name
            create_categories_sql = (
                '''CREATE TABLE IF NOT EXISTS categories (
                    cat_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cat_name VARCHAR(100) NOT NULL,
                    cat_desc TEXT NULL,
                    cat_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )'''
                if dialect_name == 'sqlite'
                else '''CREATE TABLE IF NOT EXISTS categories (
                    cat_id INT AUTO_INCREMENT PRIMARY KEY,
                    cat_name VARCHAR(100) NOT NULL,
                    cat_desc TEXT NULL,
                    cat_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )'''
            )

            if not inspector.has_table('categories'):
                db.session.execute(text(create_categories_sql))
                if inspector.has_table('category'):
                    db.session.execute(
                        text(
                            '''INSERT INTO categories (cat_name, cat_desc)
                               SELECT c.cat_name, c.cat_desc
                               FROM category c
                               WHERE c.cat_name IS NOT NULL AND TRIM(c.cat_name) <> ''
                               AND NOT EXISTS (
                                   SELECT 1 FROM categories x
                                   WHERE LOWER(TRIM(x.cat_name)) = LOWER(TRIM(c.cat_name))
                               )'''
                        )
                    )
                db.session.commit()

            inspector = inspect(db.engine)
            category_columns = {col['name'] for col in inspector.get_columns('categories')}

            if 'cat_name' not in category_columns:
                db.session.execute(text('ALTER TABLE categories ADD COLUMN cat_name VARCHAR(100) NOT NULL'))
            if 'cat_desc' not in category_columns:
                db.session.execute(text('ALTER TABLE categories ADD COLUMN cat_desc TEXT NULL'))
            if 'cat_date' not in category_columns:
                db.session.execute(text('ALTER TABLE categories ADD COLUMN cat_date DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP'))
            db.session.commit()

            inspector = inspect(db.engine)
            category_indexes = {idx.get('name') for idx in inspector.get_indexes('categories')}
            if 'uq_categories_cat_name' not in category_indexes:
                try:
                    db.session.execute(text('ALTER TABLE categories ADD UNIQUE KEY uq_categories_cat_name (cat_name)'))
                    db.session.commit()
                except Exception:
                    db.session.rollback()

            if not inspector.has_table('property'):
                return

            property_columns = {col['name'] for col in inspector.get_columns('property')}
            if 'category_id' not in property_columns:
                db.session.execute(text('ALTER TABLE property ADD COLUMN category_id INT NULL'))
                db.session.commit()

            db.session.execute(
                text(
                    '''INSERT INTO categories (cat_name, cat_desc)
                       SELECT DISTINCT TRIM(p.prop_type) AS cat_name, 'Auto-migrated from property types'
                       FROM property p
                       WHERE p.prop_type IS NOT NULL AND TRIM(p.prop_type) <> ''
                       AND NOT EXISTS (
                           SELECT 1 FROM categories c
                           WHERE LOWER(TRIM(c.cat_name)) = LOWER(TRIM(p.prop_type))
                       )'''
                )
            )

            if dialect_name == 'sqlite':
                db.session.execute(
                    text(
                        '''UPDATE property
                           SET category_id = (
                               SELECT c.cat_id
                               FROM categories c
                               WHERE LOWER(TRIM(c.cat_name)) = LOWER(TRIM(property.prop_type))
                               LIMIT 1
                           )
                           WHERE (category_id IS NULL OR category_id = 0)
                           AND prop_type IS NOT NULL AND TRIM(prop_type) <> ''
                           AND EXISTS (
                               SELECT 1 FROM categories c
                               WHERE LOWER(TRIM(c.cat_name)) = LOWER(TRIM(property.prop_type))
                           )'''
                    )
                )
            else:
                db.session.execute(
                    text(
                        '''UPDATE property p
                           JOIN categories c ON LOWER(TRIM(c.cat_name)) = LOWER(TRIM(p.prop_type))
                           SET p.category_id = c.cat_id
                           WHERE (p.category_id IS NULL OR p.category_id = 0)
                           AND p.prop_type IS NOT NULL AND TRIM(p.prop_type) <> '' '''
                    )
                )

            db.session.execute(
                text(
                    '''UPDATE property
                       SET category_id = NULL
                       WHERE category_id IS NOT NULL
                       AND category_id NOT IN (SELECT cat_id FROM categories)'''
                )
            )
            db.session.commit()

            inspector = inspect(db.engine)
            property_indexes = {idx.get('name') for idx in inspector.get_indexes('property')}
            if 'idx_property_category_id' not in property_indexes:
                try:
                    db.session.execute(text('ALTER TABLE property ADD INDEX idx_property_category_id (category_id)'))
                    db.session.commit()
                except Exception:
                    db.session.rollback()

            fks = inspector.get_foreign_keys('property')
            has_category_fk = any(
                fk.get('referred_table') == 'categories'
                and 'category_id' in (fk.get('constrained_columns') or [])
                for fk in fks
            )
            if not has_category_fk:
                try:
                    db.session.execute(
                        text(
                            '''ALTER TABLE property
                               ADD CONSTRAINT fk_property_category_id
                               FOREIGN KEY (category_id) REFERENCES categories(cat_id)
                               ON UPDATE CASCADE ON DELETE RESTRICT'''
                        )
                    )
                    db.session.commit()
                except Exception:
                    db.session.rollback()
        except (OperationalError, SQLAlchemyError) as exc:
            db.session.rollback()
            print(f'Skipping category schema compatibility due to database error: {exc}')


def initialize_database():
    """Optional runtime initializer. Call explicitly after app import when needed."""
    with app.app_context():
        try:
            db.create_all()
            ensure_admin_schema_compatibility()
            ensure_category_schema_compatibility()
        except (OperationalError, SQLAlchemyError) as exc:
            db.session.rollback()
            print(f'Database initialization skipped: {exc}')


from pkg import user_routes, admin_routes
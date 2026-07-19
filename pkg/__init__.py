import cloudinary
import cloudinary.uploader
import cloudinary.api

from flask import Flask
from flask_mail import Mail
from pkg.models import db
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
import os


app = Flask(__name__, instance_relative_config=True)

# Load instance configuration before initializing extensions.
os.makedirs(app.instance_path, exist_ok=True)
app.config.from_object('pkg.config')
print("=" * 80)
print("RUNNING FILE:", __file__)
print("DATABASE_URL ENV =", os.getenv("DATABASE_URL"))
print("MYSQL_URL ENV =", os.getenv("MYSQL_URL"))
print("INSTANCE PATH =", app.instance_path)
print("INSTANCE CONFIG EXISTS =", os.path.exists(os.path.join(app.instance_path, "config.py")))
print("CONFIG DATABASE_URL =", app.config.get("DATABASE_URL"))
print("SQLALCHEMY_DATABASE_URI =", app.config.get("SQLALCHEMY_DATABASE_URI"))
print("=" * 80)

app.config.from_pyfile('config.py', silent=True)
print("=" * 80)
print("RUNNING FILE:", __file__)
print("DATABASE_URL ENV =", os.getenv("DATABASE_URL"))
print("MYSQL_URL ENV =", os.getenv("MYSQL_URL"))
print("INSTANCE PATH =", app.instance_path)
print("INSTANCE CONFIG EXISTS =", os.path.exists(os.path.join(app.instance_path, "config.py")))
print("CONFIG DATABASE_URL =", app.config.get("DATABASE_URL"))
print("SQLALCHEMY_DATABASE_URI =", app.config.get("SQLALCHEMY_DATABASE_URI"))
print("=" * 80)

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', app.config.get('SECRET_KEY', 'securedkey'))

app.config.setdefault('SQLALCHEMY_TRACK_MODIFICATIONS', False)

app.config.setdefault('MAIL_SERVER', '127.0.0.1')
app.config.setdefault('MAIL_PORT', 25)
app.config.setdefault('MAIL_USE_TLS', False)
app.config.setdefault('MAIL_USE_SSL', False)
app.config.setdefault('MAIL_USERNAME', None)
app.config.setdefault('MAIL_PASSWORD', None)
app.config.setdefault('MAIL_DEFAULT_SENDER', 'noreply@kayhomes.local')

# Uploads
UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads')
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Ensure upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
print("=" * 80)
print("RUNNING FILE:", __file__)
print("DATABASE_URL ENV =", os.getenv("DATABASE_URL"))
print("MYSQL_URL ENV =", os.getenv("MYSQL_URL"))
print("INSTANCE PATH =", app.instance_path)
print("INSTANCE CONFIG EXISTS =", os.path.exists(os.path.join(app.instance_path, "config.py")))
print("CONFIG DATABASE_URL =", app.config.get("DATABASE_URL"))
print("SQLALCHEMY_DATABASE_URI =", app.config.get("SQLALCHEMY_DATABASE_URI"))
print("=" * 80)

db.init_app(app)
mail = Mail(app)


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
                inspector = inspect(db.engine)
                property_columns = {col['name'] for col in inspector.get_columns('property')}

            if 'prop_lga' not in property_columns:
                db.session.execute(text('ALTER TABLE property ADD COLUMN prop_lga VARCHAR(120) NULL'))
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


def ensure_user_theme_schema_compatibility():
    """Ensure users table has a theme column with safe default values."""
    with app.app_context():
        try:
            inspector = inspect(db.engine)
            if not inspector.has_table('users'):
                return

            columns = {col['name'] for col in inspector.get_columns('users')}
            schema_changed = False

            if 'theme' not in columns:
                db.session.execute(text("ALTER TABLE users ADD COLUMN theme VARCHAR(20) NOT NULL DEFAULT 'light'"))
                schema_changed = True

            db.session.execute(
                text("UPDATE users SET theme = 'light' WHERE theme IS NULL OR theme NOT IN ('light', 'dark')")
            )

            if schema_changed:
                db.session.commit()
            else:
                db.session.flush()
                db.session.commit()
        except (OperationalError, SQLAlchemyError) as exc:
            db.session.rollback()
            print(f'Skipping user theme schema compatibility due to database error: {exc}')


def ensure_runtime_tables_compatibility():
    """Create and repair non-model tables required by user routes without data loss."""
    with app.app_context():
        try:
            inspector = inspect(db.engine)
            dialect = db.engine.dialect.name

            auto_pk = 'INTEGER PRIMARY KEY AUTOINCREMENT' if dialect == 'sqlite' else 'INT AUTO_INCREMENT PRIMARY KEY'
            bool_type = 'INTEGER' if dialect == 'sqlite' else 'TINYINT(1)'
            now_default = 'CURRENT_TIMESTAMP'

            create_messages_sql = f'''
                CREATE TABLE IF NOT EXISTS messages (
                    msg_id {auto_pk},
                    sender_id INT NOT NULL,
                    receiver_id INT NOT NULL,
                    property_id INT NOT NULL,
                    message TEXT NOT NULL,
                    is_read {bool_type} NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT {now_default}
                )
            '''
            create_inquiries_sql = f'''
                CREATE TABLE IF NOT EXISTS inquiries (
                    inqu_id {auto_pk},
                    inqu_mssg TEXT NULL,
                    inqu_date DATETIME NOT NULL DEFAULT {now_default},
                    inqu_userid INT NULL,
                    inqu_propid INT NULL
                )
            '''
            create_property_image_sql = f'''
                CREATE TABLE IF NOT EXISTS property_image (
                    pimg_id {auto_pk},
                    pimg_url VARCHAR(255) NOT NULL,
                    pimg_propid INT NOT NULL
                )
            '''

            if not inspector.has_table('messages'):
                db.session.execute(text(create_messages_sql))
                db.session.commit()

            if not inspector.has_table('inquiries'):
                db.session.execute(text(create_inquiries_sql))
                db.session.commit()

            if not inspector.has_table('property_image'):
                db.session.execute(text(create_property_image_sql))
                db.session.commit()

            inspector = inspect(db.engine)

            if inspector.has_table('messages'):
                msg_cols = {col['name'] for col in inspector.get_columns('messages')}
                required_message_cols = {
                    'sender_id': 'INT NOT NULL DEFAULT 0',
                    'receiver_id': 'INT NOT NULL DEFAULT 0',
                    'property_id': 'INT NOT NULL DEFAULT 0',
                    'message': 'TEXT NULL',
                    'is_read': f'{bool_type} NOT NULL DEFAULT 0',
                    'created_at': f'DATETIME NOT NULL DEFAULT {now_default}',
                }
                for col_name, col_type in required_message_cols.items():
                    if col_name not in msg_cols:
                        db.session.execute(text(f'ALTER TABLE messages ADD COLUMN {col_name} {col_type}'))
                db.session.commit()

            if inspector.has_table('inquiries'):
                inq_cols = {col['name'] for col in inspector.get_columns('inquiries')}
                required_inquiry_cols = {
                    'inqu_mssg': 'TEXT NULL',
                    'inqu_date': f'DATETIME NOT NULL DEFAULT {now_default}',
                    'inqu_userid': 'INT NULL',
                    'inqu_propid': 'INT NULL',
                }
                for col_name, col_type in required_inquiry_cols.items():
                    if col_name not in inq_cols:
                        db.session.execute(text(f'ALTER TABLE inquiries ADD COLUMN {col_name} {col_type}'))
                db.session.commit()

            if inspector.has_table('favorites'):
                fav_cols = {col['name'] for col in inspector.get_columns('favorites')}
                if 'fav_userid' not in fav_cols:
                    db.session.execute(text('ALTER TABLE favorites ADD COLUMN fav_userid INT NULL'))
                if 'fav_propid' not in fav_cols:
                    db.session.execute(text('ALTER TABLE favorites ADD COLUMN fav_propid INT NULL'))
                db.session.commit()

            if inspector.has_table('property_image'):
                pimg_cols = {col['name'] for col in inspector.get_columns('property_image')}
                if 'property_id' not in pimg_cols and 'pimg_propid' not in pimg_cols:
                    db.session.execute(text('ALTER TABLE property_image ADD COLUMN pimg_propid INT NULL'))
                if 'image_path' not in pimg_cols and 'pimg_url' not in pimg_cols:
                    db.session.execute(text('ALTER TABLE property_image ADD COLUMN pimg_url VARCHAR(255) NULL'))
                db.session.commit()
        except (OperationalError, SQLAlchemyError) as exc:
            db.session.rollback()
            print(f'Skipping runtime tables compatibility due to database error: {exc}')


def initialize_database():
    """Optional runtime initializer. Call explicitly after app import when needed."""
    with app.app_context():
        try:
            db.create_all()
            ensure_admin_schema_compatibility()
            ensure_category_schema_compatibility()
            ensure_user_theme_schema_compatibility()
            ensure_runtime_tables_compatibility()
        except (OperationalError, SQLAlchemyError) as exc:
            db.session.rollback()
            print(f'Database initialization skipped: {exc}')


from pkg import user_routes, admin_routes
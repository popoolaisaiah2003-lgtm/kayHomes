import cloudinary
import cloudinary.uploader
import cloudinary.api
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

from flask import Flask, render_template
from flask_mail import Mail
from pkg.models import db
from sqlalchemy import inspect, text
from sqlalchemy.exc import OperationalError, SQLAlchemyError
import os


app = Flask(__name__, instance_relative_config=True)

# Load instance configuration before initializing extensions.
os.makedirs(app.instance_path, exist_ok=True)
app.config.from_object('pkg.config')

app.config.from_pyfile('config.py', silent=True)

cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True
)

app.config['SECRET_KEY'] = app.config.get('SECRET_KEY')

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
AVATAR_UPLOAD_FOLDER = os.path.join(app.root_path, 'static', 'uploads', 'avatars')
app.config['AVATAR_UPLOAD_FOLDER'] = AVATAR_UPLOAD_FOLDER

# Ensure upload folder exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)
os.makedirs(app.config['AVATAR_UPLOAD_FOLDER'], exist_ok=True)

db.init_app(app)
mail = Mail(app)


def format_naira(value):
    if value is None:
        return '₦0'

    cleaned = value
    if isinstance(value, str):
        cleaned = value.replace('₦', '').replace(',', '').strip()
        if cleaned == '':
            return '₦0'

    try:
        amount = Decimal(str(cleaned))
    except (InvalidOperation, ValueError, TypeError):
        return str(value)

    rounded = amount.quantize(Decimal('1'), rounding=ROUND_HALF_UP)
    return f"₦{int(rounded):,}"


@app.template_filter('naira')
def naira_filter(value):
    return format_naira(value)


@app.errorhandler(404)
def handle_not_found(error):
    return render_template('user/404.html', error=error), 404


@app.errorhandler(500)
def handle_internal_error(error):
    db.session.rollback()
    return render_template('user/500.html', error=error), 500


@app.errorhandler(503)
def handle_service_unavailable(error):
    return render_template('user/503.html', error=error), 503


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
            app.logger.warning('Skipping admin schema compatibility due to database error: %s', exc)


_CATEGORY_SCHEMA_ENSURED = False


NIGERIAN_STATES = [
    'Abia', 'Adamawa', 'Akwa Ibom', 'Anambra', 'Bauchi', 'Bayelsa', 'Benue', 'Borno',
    'Cross River', 'Delta', 'Ebonyi', 'Edo', 'Ekiti', 'Enugu', 'Gombe', 'Imo',
    'Jigawa', 'Kaduna', 'Kano', 'Katsina', 'Kebbi', 'Kogi', 'Kwara', 'Lagos',
    'Nasarawa', 'Niger', 'Ogun', 'Ondo', 'Osun', 'Oyo', 'Plateau', 'Rivers',
    'Sokoto', 'Taraba', 'Yobe', 'Zamfara', 'Abuja (FCT)'
]


SEED_LGAS_BY_STATE = {
    'Abia': ['Aba North', 'Aba South', 'Umuahia North', 'Umuahia South', 'Arochukwu'],
    'Adamawa': ['Yola North', 'Yola South', 'Mubi North', 'Mubi South', 'Jimeta'],
    'Akwa Ibom': ['Uyo', 'Eket', 'Ikot Ekpene', 'Oron', 'Abak'],
    'Lagos': ['Ikeja', 'Eti-Osa', 'Surulere', 'Lagos Island', 'Ikorodu', 'Alimosho'],
    'Abuja (FCT)': ['Abuja Municipal', 'Gwagwalada', 'Kwali', 'Kuje', 'Bwari'],
    'Cross River': ['Calabar Municipal', 'Calabar South', 'Ikom', 'Ogoja', 'Obudu'],
    'Ebonyi': ['Abakaliki', 'Afikpo North', 'Afikpo South', 'Onicha', 'Ohaozara'],
    'Edo': ['Oredo', 'Egor', 'Ikpoba-Okha', 'Ovia North-East', 'Esan West'],
    'Gombe': ['Gombe', 'Akko', 'Billiri', 'Dukku', 'Yamaltu-Deba'],
    'Jigawa': ['Dutse', 'Hadejia', 'Kazaure', 'Gumel', 'Ringim'],
    'Oyo': ['Ibadan North', 'Ibadan South-West', 'Ogbomoso North', 'Oyo East', 'Iseyin'],
    'Kebbi': ['Birnin Kebbi', 'Argungu', 'Yauri', 'Zuru', 'Jega'],
    'Ogun': ['Abeokuta North', 'Abeokuta South', 'Ifo', 'Sagamu', 'Ijebu East'],
    'Osun': ['Osogbo', 'Ife Central', 'Ilesa East', 'Ede North', 'Ikire'],
    'Plateau': ['Jos North', 'Jos South', 'Barkin Ladi', 'Mangu', 'Pankshin'],
    'Rivers': ['Port Harcourt', 'Obio-Akpor', 'Eleme', 'Okrika', 'Ikwerre'],
    'Anambra': ['Awka South', 'Awka North', 'Onitsha North', 'Onitsha South', 'Nnewi North'],
    'Enugu': ['Enugu North', 'Enugu South', 'Enugu East', 'Awgu', 'Aninri'],
    'Delta': ['Warri South', 'Warri North', 'Ethiope East', 'Sapele', 'Ughelli South'],
    'Kaduna': ['Kaduna North', 'Kaduna South', 'Zaria', 'Kachia', 'Jemaa'],
    'Kano': ['Kano Municipal', 'Fagge', 'Gwale', 'Dala', 'Nassarawa'],
    'Katsina': ['Katsina', 'Funtua', 'Daura', 'Bakori', 'Dutsin-Ma'],
    'Bauchi': ['Bauchi', 'Azare', 'Ningi', 'Misau', 'Jama\'are'],
    'Borno': ['Maiduguri', 'Bama', 'Dikwa', 'Gwoza', 'Kaga'],
    'Benue': ['Makurdi', 'Gboko', 'Otukpo', 'Vandeikya', 'Katsina-Ala'],
    'Bayelsa': ['Yenagoa', 'Nembe', 'Brass', 'Ogbia', 'Sagbama'],
    'Imo': ['Owerri Municipal', 'Orlu', 'Okigwe', 'Mbaitoli', 'Aboh Mbaise'],
    'Ondo': ['Akure South', 'Akure North', 'Owo', 'Ondo East', 'Ondo West'],
    'Ekiti': ['Ado Ekiti', 'Ikere', 'Oye', 'Irepodun/Ifelodun', 'Emure'],
    'Kogi': ['Lokoja', 'Okene', 'Idah', 'Ankpa', 'Bassa'],
    'Kwara': ['Ilorin East', 'Ilorin West', 'Offa', 'Omu-Aran', 'Kaiama'],
    'Niger': ['Minna', 'Suleja', 'Kontagora', 'Bida', 'Zungeru'],
    'Nasarawa': ['Lafia', 'Akwanga', 'Keffi', 'Kokona', 'Doma'],
    'Sokoto': ['Sokoto North', 'Sokoto South', 'Gwadabawa', 'Tambuwal', 'Wurno'],
    'Taraba': ['Jalingo', 'Wukari', 'Lau', 'Bali', 'Gashaka'],
    'Yobe': ['Damaturu', 'Potiskum', 'Gashua', 'Bade', 'Fika'],
    'Zamfara': ['Gusau', 'Anka', 'Bakura', 'Bungudu', 'Maradun'],

}


def ensure_category_schema_compatibility():
    """Ensure categories are database-driven and linked to properties."""
    global _CATEGORY_SCHEMA_ENSURED
    if _CATEGORY_SCHEMA_ENSURED:
        return

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
            app.logger.warning('Skipping category schema compatibility due to database error: %s', exc)

    _CATEGORY_SCHEMA_ENSURED = True


def ensure_user_theme_schema_compatibility():
    """Ensure users table has theme/avatar/verification columns with safe defaults."""
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

            if 'user_avatar' not in columns:
                db.session.execute(text('ALTER TABLE users ADD COLUMN user_avatar VARCHAR(255) NULL'))
                schema_changed = True

            if 'user_verified' not in columns:
                db.session.execute(text('ALTER TABLE users ADD COLUMN user_verified TINYINT(1) NOT NULL DEFAULT 0'))
                schema_changed = True

            db.session.execute(
                text("UPDATE users SET theme = 'light' WHERE theme IS NULL OR theme NOT IN ('light', 'dark')")
            )
            db.session.execute(
                text('UPDATE users SET user_verified = 0 WHERE user_verified IS NULL')
            )

            if schema_changed:
                db.session.commit()
            else:
                db.session.flush()
                db.session.commit()
        except (OperationalError, SQLAlchemyError) as exc:
            db.session.rollback()
            app.logger.warning('Skipping user theme schema compatibility due to database error: %s', exc)


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
            create_saved_searches_sql = f'''
                CREATE TABLE IF NOT EXISTS saved_searches (
                    search_id {auto_pk},
                    user_id INT NOT NULL,
                    name VARCHAR(150) NOT NULL,
                    q VARCHAR(255) NULL,
                    state VARCHAR(120) NULL,
                    lga VARCHAR(120) NULL,
                    property_type VARCHAR(120) NULL,
                    bedrooms INT NULL,
                    bathrooms INT NULL,
                    min_price INT NULL,
                    max_price INT NULL,
                    furnished VARCHAR(20) NULL,
                    sort VARCHAR(20) NULL,
                    created_at DATETIME NOT NULL DEFAULT {now_default},
                    CONSTRAINT fk_saved_searches_user
                        FOREIGN KEY (user_id) REFERENCES users(user_id)
                        ON DELETE CASCADE ON UPDATE CASCADE
                )
            '''
            create_notifications_sql = f'''
                CREATE TABLE IF NOT EXISTS notifications (
                    notification_id {auto_pk},
                    user_id INT NOT NULL,
                    type VARCHAR(40) NOT NULL,
                    title VARCHAR(150) NOT NULL,
                    message VARCHAR(255) NOT NULL,
                    link VARCHAR(255) NULL,
                    is_read {bool_type} NOT NULL DEFAULT 0,
                    created_at DATETIME NOT NULL DEFAULT {now_default},
                    CONSTRAINT fk_notifications_user
                        FOREIGN KEY (user_id) REFERENCES users(user_id)
                        ON DELETE CASCADE ON UPDATE CASCADE
                )
            '''
            create_property_view_events_sql = f'''
                CREATE TABLE IF NOT EXISTS property_view_events (
                    view_event_id {auto_pk},
                    property_id INT NOT NULL,
                    owner_id INT NOT NULL,
                    viewer_id INT NULL,
                    viewed_at DATETIME NOT NULL DEFAULT {now_default},
                    CONSTRAINT fk_view_events_property
                        FOREIGN KEY (property_id) REFERENCES property(prop_id)
                        ON DELETE CASCADE ON UPDATE CASCADE,
                    CONSTRAINT fk_view_events_owner
                        FOREIGN KEY (owner_id) REFERENCES users(user_id)
                        ON DELETE CASCADE ON UPDATE CASCADE
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

            if not inspector.has_table('saved_searches'):
                db.session.execute(text(create_saved_searches_sql))
                db.session.commit()

            if not inspector.has_table('notifications'):
                db.session.execute(text(create_notifications_sql))
                db.session.commit()

            if not inspector.has_table('property_view_events'):
                db.session.execute(text(create_property_view_events_sql))
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
                if 'created_at' not in fav_cols:
                    db.session.execute(text(f'ALTER TABLE favorites ADD COLUMN created_at DATETIME NOT NULL DEFAULT {now_default}'))
                db.session.commit()

            if inspector.has_table('property'):
                prop_cols = {col['name'] for col in inspector.get_columns('property')}
                required_cols = {
                    'prop_bedroom': 'ALTER TABLE property ADD COLUMN prop_bedroom INT NULL',
                    'prop_bathroom': 'ALTER TABLE property ADD COLUMN prop_bathroom INT NULL',
                    'prop_toilet': 'ALTER TABLE property ADD COLUMN prop_toilet INT NULL',
                    'prop_area': 'ALTER TABLE property ADD COLUMN prop_area INT NULL',
                    'prop_area_unit': "ALTER TABLE property ADD COLUMN prop_area_unit VARCHAR(20) NULL DEFAULT 'sqm'",
                    'bedrooms': 'ALTER TABLE property ADD COLUMN bedrooms INT NULL',
                    'bathrooms': 'ALTER TABLE property ADD COLUMN bathrooms INT NULL',
                    'toilets': 'ALTER TABLE property ADD COLUMN toilets INT NULL',
                    'area_sqm': 'ALTER TABLE property ADD COLUMN area_sqm INT NULL',
                    'prop_status': "ALTER TABLE property ADD COLUMN prop_status VARCHAR(20) NOT NULL DEFAULT 'available'",
                    'prop_views': 'ALTER TABLE property ADD COLUMN prop_views INT NOT NULL DEFAULT 0',
                }
                for col_name, alter_stmt in required_cols.items():
                    if col_name not in prop_cols:
                        try:
                            db.session.execute(text(alter_stmt))
                            db.session.commit()
                        except Exception:
                            db.session.rollback()

                try:
                    db.session.execute(text('UPDATE property SET bedrooms = prop_bedroom WHERE bedrooms IS NULL AND prop_bedroom IS NOT NULL'))
                    db.session.execute(text('UPDATE property SET bathrooms = prop_bathroom WHERE bathrooms IS NULL AND prop_bathroom IS NOT NULL'))
                    db.session.execute(text('UPDATE property SET toilets = prop_toilet WHERE toilets IS NULL AND prop_toilet IS NOT NULL'))
                    db.session.execute(text('UPDATE property SET area_sqm = prop_area WHERE area_sqm IS NULL AND prop_area IS NOT NULL'))
                    db.session.execute(
                        text(
                            "UPDATE property SET prop_status = 'available' "
                            "WHERE prop_status IS NULL OR LOWER(TRIM(prop_status)) NOT IN ('available', 'pending', 'rented')"
                        )
                    )
                    db.session.execute(
                        text(
                            'UPDATE property SET prop_views = 0 '
                            'WHERE prop_views IS NULL OR prop_views < 0'
                        )
                    )
                    db.session.commit()
                except Exception:
                    db.session.rollback()

                prop_cols = {col['name'] for col in inspect(db.engine).get_columns('property')}

                prop_indexes = {idx['name'] for idx in inspector.get_indexes('property')}
                if 'idx_property_category_id' not in prop_indexes and 'category_id' in prop_cols:
                    try:
                        db.session.execute(text('CREATE INDEX idx_property_category_id ON property(category_id)'))
                        db.session.commit()
                    except Exception:
                        db.session.rollback()

                if 'idx_property_prop_state' not in prop_indexes and 'prop_state' in prop_cols:
                    try:
                        db.session.execute(text('CREATE INDEX idx_property_prop_state ON property(prop_state)'))
                        db.session.commit()
                    except Exception:
                        db.session.rollback()

            if inspector.has_table('saved_searches'):
                saved_cols = {col['name'] for col in inspector.get_columns('saved_searches')}
                required_saved_cols = {
                    'user_id': 'INT NOT NULL',
                    'name': 'VARCHAR(150) NOT NULL',
                    'q': 'VARCHAR(255) NULL',
                    'state': 'VARCHAR(120) NULL',
                    'lga': 'VARCHAR(120) NULL',
                    'property_type': 'VARCHAR(120) NULL',
                    'bedrooms': 'INT NULL',
                    'bathrooms': 'INT NULL',
                    'min_price': 'INT NULL',
                    'max_price': 'INT NULL',
                    'furnished': 'VARCHAR(20) NULL',
                    'sort': 'VARCHAR(20) NULL',
                    'created_at': f'DATETIME NOT NULL DEFAULT {now_default}',
                }
                for col_name, col_type in required_saved_cols.items():
                    if col_name not in saved_cols:
                        db.session.execute(text(f'ALTER TABLE saved_searches ADD COLUMN {col_name} {col_type}'))
                db.session.commit()

            if inspector.has_table('notifications'):
                notification_cols = {col['name'] for col in inspector.get_columns('notifications')}
                required_notification_cols = {
                    'user_id': 'INT NOT NULL',
                    'type': 'VARCHAR(40) NOT NULL',
                    'title': 'VARCHAR(150) NOT NULL',
                    'message': 'VARCHAR(255) NOT NULL',
                    'link': 'VARCHAR(255) NULL',
                    'is_read': f'{bool_type} NOT NULL DEFAULT 0',
                    'created_at': f'DATETIME NOT NULL DEFAULT {now_default}',
                }
                for col_name, col_type in required_notification_cols.items():
                    if col_name not in notification_cols:
                        db.session.execute(text(f'ALTER TABLE notifications ADD COLUMN {col_name} {col_type}'))
                db.session.commit()

                notification_indexes = {idx['name'] for idx in inspector.get_indexes('notifications')}
                if 'idx_notifications_user_created' not in notification_indexes:
                    try:
                        db.session.execute(text('CREATE INDEX idx_notifications_user_created ON notifications(user_id, created_at)'))
                        db.session.commit()
                    except Exception:
                        db.session.rollback()

            if inspector.has_table('property_view_events'):
                view_event_cols = {col['name'] for col in inspector.get_columns('property_view_events')}
                required_view_event_cols = {
                    'property_id': 'INT NOT NULL',
                    'owner_id': 'INT NOT NULL',
                    'viewer_id': 'INT NULL',
                    'viewed_at': f'DATETIME NOT NULL DEFAULT {now_default}',
                }
                for col_name, col_type in required_view_event_cols.items():
                    if col_name not in view_event_cols:
                        db.session.execute(text(f'ALTER TABLE property_view_events ADD COLUMN {col_name} {col_type}'))
                db.session.commit()

                view_event_indexes = {idx['name'] for idx in inspector.get_indexes('property_view_events')}
                if 'idx_view_events_owner_date' not in view_event_indexes:
                    try:
                        db.session.execute(text('CREATE INDEX idx_view_events_owner_date ON property_view_events(owner_id, viewed_at)'))
                        db.session.commit()
                    except Exception:
                        db.session.rollback()

            ensure_property_reviews_table()
        except (OperationalError, SQLAlchemyError) as exc:
            db.session.rollback()
            app.logger.warning('Skipping runtime tables compatibility due to database error: %s', exc)


def ensure_property_reviews_table():
    with app.app_context():
        try:
            inspector = inspect(db.engine)
            if not inspector.has_table('property_reviews'):
                db.session.execute(text('''
                    CREATE TABLE property_reviews (
                        review_id INT AUTO_INCREMENT PRIMARY KEY,
                        property_id INT NOT NULL,
                        reviewer_id INT NOT NULL,
                        owner_id INT NOT NULL,
                        rating INT NOT NULL,
                        review_text TEXT NULL,
                        review_tags VARCHAR(255) NULL,
                        is_visible TINYINT(1) DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT fk_rev_property FOREIGN KEY (property_id) REFERENCES property(prop_id) ON DELETE CASCADE,
                        CONSTRAINT fk_rev_reviewer FOREIGN KEY (reviewer_id) REFERENCES users(user_id) ON DELETE CASCADE,
                        CONSTRAINT fk_rev_owner FOREIGN KEY (owner_id) REFERENCES users(user_id) ON DELETE CASCADE,
                        UNIQUE KEY uq_user_property_review (property_id, reviewer_id),
                        INDEX idx_rev_property_id (property_id),
                        INDEX idx_rev_owner_id (owner_id),
                        INDEX idx_rev_reviewer_id (reviewer_id),
                        INDEX idx_rev_created_at (created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                '''))
                db.session.commit()

            inspector = inspect(db.engine)
            cols = {c['name'] for c in inspector.get_columns('property_reviews')}
            required_cols = {
                'property_id': 'ALTER TABLE property_reviews ADD COLUMN property_id INT NOT NULL',
                'reviewer_id': 'ALTER TABLE property_reviews ADD COLUMN reviewer_id INT NOT NULL',
                'owner_id': 'ALTER TABLE property_reviews ADD COLUMN owner_id INT NOT NULL',
                'rating': 'ALTER TABLE property_reviews ADD COLUMN rating INT NOT NULL',
                'review_text': 'ALTER TABLE property_reviews ADD COLUMN review_text TEXT NULL',
                'review_tags': 'ALTER TABLE property_reviews ADD COLUMN review_tags VARCHAR(255) NULL',
                'is_visible': 'ALTER TABLE property_reviews ADD COLUMN is_visible TINYINT(1) DEFAULT 1',
                'created_at': 'ALTER TABLE property_reviews ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP',
            }
            for col_name, alter_sql in required_cols.items():
                if col_name not in cols:
                    db.session.execute(text(alter_sql))
            db.session.commit()

            index_rows = db.session.execute(text('SHOW INDEX FROM property_reviews')).mappings().all()
            index_map = {}
            for row in index_rows:
                key_name = row.get('Key_name')
                if key_name == 'PRIMARY':
                    continue
                seq = int(row.get('Seq_in_index') or 0)
                col_name = row.get('Column_name')
                non_unique_raw = row.get('Non_unique')
                non_unique = int(non_unique_raw) if non_unique_raw is not None else 1
                payload = index_map.setdefault(key_name, {'cols': [], 'non_unique': non_unique})
                payload['cols'].append((seq, col_name))

            normalized = {}
            for key_name, payload in index_map.items():
                ordered_cols = tuple(col for _, col in sorted(payload['cols'], key=lambda item: item[0]))
                normalized[key_name] = {'cols': ordered_cols, 'non_unique': payload['non_unique']}

            if 'uq_user_property_review' not in normalized:
                try:
                    db.session.execute(text('ALTER TABLE property_reviews ADD CONSTRAINT uq_user_property_review UNIQUE (property_id, reviewer_id)'))
                    db.session.commit()
                except Exception:
                    db.session.rollback()

            required_indexes = {
                'idx_rev_property_id': ('property_id',),
                'idx_rev_owner_id': ('owner_id',),
                'idx_rev_reviewer_id': ('reviewer_id',),
                'idx_rev_created_at': ('created_at',),
            }

            index_rows = db.session.execute(text('SHOW INDEX FROM property_reviews')).mappings().all()
            index_map = {}
            for row in index_rows:
                key_name = row.get('Key_name')
                if key_name == 'PRIMARY':
                    continue
                seq = int(row.get('Seq_in_index') or 0)
                col_name = row.get('Column_name')
                non_unique_raw = row.get('Non_unique')
                non_unique = int(non_unique_raw) if non_unique_raw is not None else 1
                payload = index_map.setdefault(key_name, {'cols': [], 'non_unique': non_unique})
                payload['cols'].append((seq, col_name))

            normalized = {}
            for key_name, payload in index_map.items():
                ordered_cols = tuple(col for _, col in sorted(payload['cols'], key=lambda item: item[0]))
                normalized[key_name] = {'cols': ordered_cols, 'non_unique': payload['non_unique']}

            for required_name, required_cols in required_indexes.items():
                if required_name in normalized:
                    continue

                try:
                    db.session.execute(text(f'CREATE INDEX `{required_name}` ON property_reviews ({", ".join(required_cols)})'))
                    db.session.commit()
                except Exception:
                    db.session.rollback()
        except (OperationalError, SQLAlchemyError) as exc:
            db.session.rollback()
            app.logger.warning('ensure_property_reviews_table skipped or failed: %s', exc)


def ensure_state_lga_seed_data():
    with app.app_context():
        try:
            inspector = inspect(db.engine)
            if not inspector.has_table('state') or not inspector.has_table('lga'):
                return

            state_count = db.session.execute(text('SELECT COUNT(*) FROM state')).scalar() or 0
            if int(state_count) == 0:
                for state_name in NIGERIAN_STATES:
                    db.session.execute(
                        text('INSERT INTO state (state_name) VALUES (:state_name)'),
                        {'state_name': state_name},
                    )
                db.session.commit()

            state_rows = db.session.execute(text('SELECT state_id, state_name FROM state')).mappings().all()
            state_id_map = {str(row['state_name']).strip(): int(row['state_id']) for row in state_rows}

            # Insert only missing LGAs for each state; never duplicate existing state/LGA pairs.
            for state_name, lgas in SEED_LGAS_BY_STATE.items():
                state_id = state_id_map.get(state_name)
                if not state_id:
                    continue

                for lga_name in lgas:
                    clean_lga = (lga_name or '').strip()
                    if not clean_lga:
                        continue

                    db.session.execute(
                        text(
                            '''INSERT INTO lga (lga_name, lga_stateid)
                               SELECT :lga_name, :state_id
                               WHERE NOT EXISTS (
                                   SELECT 1
                                   FROM lga
                                   WHERE lga_stateid = :state_id
                                     AND LOWER(TRIM(lga_name)) = LOWER(TRIM(:lga_name))
                               )'''
                        ),
                        {'lga_name': clean_lga, 'state_id': state_id},
                    )
            db.session.commit()

            lga_indexes = {idx.get('name') for idx in inspector.get_indexes('lga')}
            if 'lga_stateid_idx' not in lga_indexes:
                try:
                    db.session.execute(text('CREATE INDEX lga_stateid_idx ON lga(lga_stateid)'))
                    db.session.commit()
                except Exception:
                    db.session.rollback()

            inspector = inspect(db.engine)
            lga_fks = inspector.get_foreign_keys('lga')
            has_state_fk = any(
                fk.get('referred_table') == 'state'
                and 'lga_stateid' in (fk.get('constrained_columns') or [])
                and 'state_id' in (fk.get('referred_columns') or [])
                for fk in lga_fks
            )
            if not has_state_fk:
                try:
                    db.session.execute(
                        text(
                            '''ALTER TABLE lga
                               ADD CONSTRAINT lga_stateid
                               FOREIGN KEY (lga_stateid) REFERENCES state(state_id)
                               ON DELETE CASCADE ON UPDATE CASCADE'''
                        )
                    )
                    db.session.commit()
                except Exception:
                    db.session.rollback()
        except (OperationalError, SQLAlchemyError) as exc:
            db.session.rollback()
            app.logger.warning('Skipping state/lga seed compatibility due to database error: %s', exc)


def ensure_startup_schema_compatibility():
    """Production-safe compatibility patch for Railway MySQL before requests are handled."""
    with app.app_context():
        try:
            dialect = db.engine.dialect.name
            if dialect != 'mysql':
                return

            db_name = db.session.execute(text('SELECT DATABASE()')).scalar()
            if not db_name:
                return

            def table_exists(table_name):
                return bool(
                    db.session.execute(
                        text(
                            '''
                            SELECT 1
                            FROM INFORMATION_SCHEMA.TABLES
                            WHERE TABLE_SCHEMA = :schema_name
                              AND TABLE_NAME = :table_name
                            LIMIT 1
                            '''
                        ),
                        {'schema_name': db_name, 'table_name': table_name},
                    ).scalar()
                )

            def column_exists(table_name, column_name):
                return bool(
                    db.session.execute(
                        text(
                            '''
                            SELECT 1
                            FROM INFORMATION_SCHEMA.COLUMNS
                            WHERE TABLE_SCHEMA = :schema_name
                              AND TABLE_NAME = :table_name
                              AND COLUMN_NAME = :column_name
                            LIMIT 1
                            '''
                        ),
                        {
                            'schema_name': db_name,
                            'table_name': table_name,
                            'column_name': column_name,
                        },
                    ).scalar()
                )

            create_statements = {
                'notifications': '''
                    CREATE TABLE IF NOT EXISTS notifications (
                        notification_id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id INT NOT NULL,
                        type VARCHAR(40) NOT NULL,
                        title VARCHAR(150) NOT NULL,
                        message VARCHAR(255) NOT NULL,
                        link VARCHAR(255) NULL,
                        is_read TINYINT(1) NOT NULL DEFAULT 0,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT fk_notifications_user
                            FOREIGN KEY (user_id) REFERENCES users(user_id)
                            ON DELETE CASCADE ON UPDATE CASCADE,
                        INDEX idx_notifications_user_created (user_id, created_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                ''',
                'saved_searches': '''
                    CREATE TABLE IF NOT EXISTS saved_searches (
                        search_id INT AUTO_INCREMENT PRIMARY KEY,
                        user_id INT NOT NULL,
                        name VARCHAR(150) NOT NULL,
                        q VARCHAR(255) NULL,
                        state VARCHAR(120) NULL,
                        lga VARCHAR(120) NULL,
                        property_type VARCHAR(120) NULL,
                        bedrooms INT NULL,
                        bathrooms INT NULL,
                        min_price INT NULL,
                        max_price INT NULL,
                        furnished VARCHAR(20) NULL,
                        sort VARCHAR(20) NULL,
                        created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT fk_saved_searches_user
                            FOREIGN KEY (user_id) REFERENCES users(user_id)
                            ON DELETE CASCADE ON UPDATE CASCADE
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                ''',
                'property_view_events': '''
                    CREATE TABLE IF NOT EXISTS property_view_events (
                        view_event_id INT AUTO_INCREMENT PRIMARY KEY,
                        property_id INT NOT NULL,
                        owner_id INT NOT NULL,
                        viewer_id INT NULL,
                        viewed_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        CONSTRAINT fk_view_events_property
                            FOREIGN KEY (property_id) REFERENCES property(prop_id)
                            ON DELETE CASCADE ON UPDATE CASCADE,
                        CONSTRAINT fk_view_events_owner
                            FOREIGN KEY (owner_id) REFERENCES users(user_id)
                            ON DELETE CASCADE ON UPDATE CASCADE,
                        INDEX idx_view_events_owner_date (owner_id, viewed_at)
                    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                ''',
            }

            for table_name, create_sql in create_statements.items():
                if not table_exists(table_name):
                    db.session.execute(text(create_sql))

            alter_statements = [
                ('users', 'user_avatar', 'ALTER TABLE users ADD COLUMN user_avatar VARCHAR(255) NULL'),
                ('users', 'user_verified', 'ALTER TABLE users ADD COLUMN user_verified TINYINT(1) NOT NULL DEFAULT 0'),
                ('property', 'prop_status', "ALTER TABLE property ADD COLUMN prop_status VARCHAR(20) NOT NULL DEFAULT 'available'"),
                ('favorites', 'created_at', 'ALTER TABLE favorites ADD COLUMN created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP'),
            ]

            for table_name, column_name, alter_sql in alter_statements:
                if table_exists(table_name) and not column_exists(table_name, column_name):
                    db.session.execute(text(alter_sql))

            db.session.commit()
        except (OperationalError, SQLAlchemyError) as exc:
            db.session.rollback()
            app.logger.warning('Startup schema compatibility skipped: %s', exc)


def initialize_database():
    """Optional runtime initializer. Call explicitly after app import when needed."""
    with app.app_context():
        try:
            db.create_all()
            ensure_admin_schema_compatibility()
            ensure_category_schema_compatibility()
            ensure_user_theme_schema_compatibility()
            ensure_startup_schema_compatibility()
            ensure_runtime_tables_compatibility()
            ensure_property_reviews_table()
            ensure_state_lga_seed_data()
        except (OperationalError, SQLAlchemyError) as exc:
            db.session.rollback()
            app.logger.warning('Database initialization skipped: %s', exc)


ensure_startup_schema_compatibility()


from pkg import user_routes, admin_routes
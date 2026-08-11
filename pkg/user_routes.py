import cloudinary
import cloudinary.uploader
from flask import render_template, request, redirect, url_for, session, flash, abort, jsonify
from flask_mail import Message
from werkzeug.security import generate_password_hash, check_password_hash
from pkg import app, ensure_category_schema_compatibility, ensure_property_reviews_table, ensure_state_lga_seed_data, format_naira, mail
from pkg.forms import ForgotPasswordForm, ResetPasswordForm
from pkg.models import Category, ContactMessage, Favorite, Notification, PasswordResetToken, PropertyReview, SavedSearch, db, User, Property
import os, secrets, time
import re
from werkzeug.utils import secure_filename
from sqlalchemy import text, inspect, or_, func, cast, Float
from urllib.parse import quote_plus
from sqlalchemy.orm import joinedload
from datetime import datetime, timedelta
from functools import wraps
from types import SimpleNamespace
from email_validator import EmailNotValidError, validate_email


MAX_PROPERTY_IMAGES = 5
ALLOWED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
ALLOWED_AVATAR_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}
MAX_AVATAR_FILE_SIZE = 10 * 1024 * 1024
ALLOWED_PROPERTY_STATUSES = {'available', 'pending', 'rented'}


_TABLE_COLUMNS_CACHE = {}
_CONTACT_MESSAGE_TABLE_ENSURED = False
_PROPERTY_IMAGE_TABLE_ENSURED = False
_PROPERTY_IMAGE_COLUMNS_CACHE = None


def get_current_user():
    user_id = session.get('user_id')
    if not user_id:
        return None
    return User.query.get(user_id)


def _normalized_theme(value):
    theme_value = (value or '').strip().lower()
    return theme_value if theme_value in {'light', 'dark'} else 'light'


def get_current_theme():
    default_theme = 'light'
    user_id = session.get('user_id')
    if not user_id:
        return default_theme

    session_theme = _normalized_theme(session.get('theme'))
    if session_theme in {'light', 'dark'} and session.get('theme'):
        return session_theme

    try:
        user = db.session.get(User, user_id)
        theme_value = _normalized_theme(user.theme if user else default_theme)
        session['theme'] = theme_value
        return theme_value
    except Exception as e:
        app.logger.exception('Unable to load theme for user %s: %s', user_id, e)
        return default_theme


def _store_next_url():
    if request.method == 'GET':
        session['next_url'] = request.url


def _redirect_after_auth(default_endpoint='home'):
    next_url = session.pop('next_url', None)
    if next_url:
        return redirect(next_url)
    return redirect(url_for(default_endpoint))


def _ensure_csrf_token():
    token = session.get('csrf_token')
    if not token:
        token = secrets.token_hex(16)
        session['csrf_token'] = token
    return token


def _submitted_csrf_token():
    return (
        request.form.get('csrf_token')
        or request.headers.get('X-CSRF-Token')
        or request.headers.get('X-CSRFToken')
    )


def _validate_csrf_request(default_endpoint='home', endpoint_values=None, expect_json=False):
    expected = session.get('csrf_token')
    submitted = _submitted_csrf_token()
    if expected and submitted and secrets.compare_digest(str(expected), str(submitted)):
        return None

    if expect_json:
        return jsonify({'error': 'Invalid CSRF token.'}), 400

    flash('Invalid CSRF token.', 'danger')
    return redirect(url_for(default_endpoint, **(endpoint_values or {})))


def _coerce_page(value, default=1):
    try:
        page = int(value)
    except (TypeError, ValueError):
        return default
    return page if page > 0 else default


def _build_pagination(total, page, per_page):
    total = int(total or 0)
    page = max(int(page or 1), 1)
    per_page = max(int(per_page or 1), 1)
    pages = max((total + per_page - 1) // per_page, 1)
    if page > pages:
        page = pages
    return SimpleNamespace(
        total=total,
        page=page,
        per_page=per_page,
        pages=pages,
        has_prev=page > 1,
        prev_num=page - 1,
        has_next=page < pages,
        next_num=page + 1,
    )


def _properties_page_params(filters, selected_category_id=None, page=None):
    params = _properties_query_params(filters)
    if selected_category_id is not None:
        params['category_id'] = selected_category_id
    if page is not None:
        params['page'] = page
    return params


def _send_password_reset_email(user, token):
    reset_url = url_for('reset_password', token=token, _external=True)
    msg = Message(
        subject='KayHomes Password Reset Request',
        recipients=[user.user_email],
        body=f"Hello {user.user_fname},\n\nTo reset your password, visit the following link:\n{reset_url}\n\nIf you did not make this request, please ignore this email.\n"
    )
    mail.send(msg)


def get_table_columns(table_name):
    if table_name in _TABLE_COLUMNS_CACHE:
        return _TABLE_COLUMNS_CACHE[table_name]
    try:
        inspector = inspect(db.engine)
        if not inspector.has_table(table_name):
            cols = set()
        else:
            cols = {column['name'] for column in inspector.get_columns(table_name)}
        _TABLE_COLUMNS_CACHE[table_name] = cols
        return cols
    except Exception:
        return set()


def ensure_contact_message_table():
    global _CONTACT_MESSAGE_TABLE_ENSURED
    if _CONTACT_MESSAGE_TABLE_ENSURED:
        return
    try:
        inspector = inspect(db.engine)
        if inspector.has_table('contact_messages'):
            _CONTACT_MESSAGE_TABLE_ENSURED = True
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
        _CONTACT_MESSAGE_TABLE_ENSURED = True
    except Exception:
        db.session.rollback()


def ensure_property_image_table():
    global _PROPERTY_IMAGE_TABLE_ENSURED
    if _PROPERTY_IMAGE_TABLE_ENSURED:
        return
    try:
        inspector = inspect(db.engine)
        if inspector.has_table('property_image'):
            cols = {column['name'] for column in inspector.get_columns('property_image')}
            fks = inspector.get_foreign_keys('property_image')

            property_ref_col = 'property_id' if 'property_id' in cols else ('pimg_propid' if 'pimg_propid' in cols else None)
            if not property_ref_col:
                _PROPERTY_IMAGE_TABLE_ENSURED = True
                return

            valid_fk = False
            for fk in fks:
                constrained = set(fk.get('constrained_columns') or [])
                if (
                    fk.get('referred_table') == 'property'
                    and 'prop_id' in (fk.get('referred_columns') or [])
                    and property_ref_col in constrained
                ):
                    valid_fk = True
                    break

            if not valid_fk:
                for fk in fks:
                    fk_name = fk.get('name')
                    if fk_name:
                        db.session.execute(text(f'ALTER TABLE property_image DROP FOREIGN KEY `{fk_name}`'))

                db.session.execute(
                    text(
                        f'''DELETE FROM property_image
                            WHERE {property_ref_col} IS NULL
                               OR {property_ref_col} NOT IN (SELECT prop_id FROM property)'''
                    )
                )

                db.session.execute(
                    text(
                        f'''ALTER TABLE property_image
                            ADD CONSTRAINT fk_propimg_propertyid
                            FOREIGN KEY ({property_ref_col}) REFERENCES property(prop_id)
                            ON DELETE CASCADE ON UPDATE CASCADE'''
                    )
                )
                db.session.commit()

            _PROPERTY_IMAGE_TABLE_ENSURED = True
            return
        db.session.execute(text('''
            CREATE TABLE IF NOT EXISTS property_image (
                image_id INT AUTO_INCREMENT PRIMARY KEY,
                property_id INT NOT NULL,
                image_path VARCHAR(255) NOT NULL,
                uploaded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                CONSTRAINT fk_property_image_property
                    FOREIGN KEY (property_id) REFERENCES property(prop_id)
                    ON DELETE CASCADE
            )
        '''))
        db.session.commit()
        _PROPERTY_IMAGE_TABLE_ENSURED = True
    except Exception:
        db.session.rollback()


def ensure_property_specs_schema():
    try:
        inspector = inspect(db.engine)
        if not inspector.has_table('property'):
            return

        columns = {col['name'] for col in inspector.get_columns('property')}
        required = {
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

        changed = False
        for col_name, ddl in required.items():
            if col_name not in columns:
                try:
                    db.session.execute(text(ddl))
                    db.session.commit()
                    changed = True
                except Exception:
                    db.session.rollback()

        if changed:
            _TABLE_COLUMNS_CACHE.pop('property', None)

        try:
            db.session.execute(
                text(
                    "UPDATE property SET prop_status = 'available' "
                    "WHERE prop_status IS NULL OR LOWER(TRIM(prop_status)) NOT IN ('available', 'pending', 'rented')"
                )
            )
            db.session.execute(
                text(
                    "UPDATE property SET prop_views = 0 "
                    "WHERE prop_views IS NULL OR prop_views < 0"
                )
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
    except Exception:
        db.session.rollback()


def get_property_image_columns():
    global _PROPERTY_IMAGE_COLUMNS_CACHE
    if _PROPERTY_IMAGE_COLUMNS_CACHE is not None:
        return _PROPERTY_IMAGE_COLUMNS_CACHE

    cols = get_table_columns('property_image')
    if not cols:
        return None
    _PROPERTY_IMAGE_COLUMNS_CACHE = {
        'id': 'image_id' if 'image_id' in cols else ('pimg_id' if 'pimg_id' in cols else None),
        'property_id': 'property_id' if 'property_id' in cols else ('pimg_propid' if 'pimg_propid' in cols else None),
        'path': 'image_path' if 'image_path' in cols else ('pimg_url' if 'pimg_url' in cols else None),
        'uploaded_at': 'uploaded_at' if 'uploaded_at' in cols else None,
    }
    return _PROPERTY_IMAGE_COLUMNS_CACHE


def is_allowed_image_extension(filename):
    _, ext = os.path.splitext(filename or '')
    return ext.lower() in ALLOWED_IMAGE_EXTENSIONS


def has_valid_image_signature(file_obj, filename):
    _, ext = os.path.splitext(filename or '')
    ext = ext.lower()
    try:
        head = file_obj.read(32)
        file_obj.seek(0)
    except Exception:
        return False

    if ext in {'.jpg', '.jpeg'}:
        return head.startswith(b'\xff\xd8\xff')
    if ext == '.png':
        return head.startswith(b'\x89PNG\r\n\x1a\n')
    if ext == '.webp':
        return len(head) >= 12 and head[0:4] == b'RIFF' and head[8:12] == b'WEBP'
    return False


def get_property_images(property_id):
    ensure_property_image_table()
    cols = get_property_image_columns()
    if not cols or not cols['property_id'] or not cols['path']:
        return []

    order_by = []
    if cols['uploaded_at']:
        order_by.append(f"{cols['uploaded_at']} ASC")
    if cols['id']:
        order_by.append(f"{cols['id']} ASC")
    if not order_by:
        order_by.append(f"{cols['path']} ASC")

    select_cols = [
        f"{cols['path']} AS image_path"
    ]
    if cols['id']:
        select_cols.insert(0, f"{cols['id']} AS image_id")
    if cols['uploaded_at']:
        select_cols.append(f"{cols['uploaded_at']} AS uploaded_at")

    stmt = text(f'''
        SELECT {', '.join(select_cols)}
        FROM property_image
        WHERE {cols['property_id']} = :pid
        ORDER BY {', '.join(order_by)}
    ''')
    try:
        rows = db.session.execute(stmt, {'pid': property_id}).mappings().all()
        return [dict(row) for row in rows]
    except Exception:
        return []


def generate_unique_upload_name(original_filename):
    _, ext = os.path.splitext(original_filename or '')
    ext = ext.lower()
    upload_path = app.config['UPLOAD_FOLDER']
    while True:
        filename = f"{secrets.token_hex(12)}{ext}"
        full_path = os.path.join(upload_path, filename)
        if not os.path.exists(full_path):
            return filename


def _cloudinary_property_upload_ready():
    return bool(
        app.config.get('CLOUDINARY_CLOUD_NAME')
        and app.config.get('CLOUDINARY_API_KEY')
        and app.config.get('CLOUDINARY_API_SECRET')
    )


def save_property_images(property_id, image_files, existing_count=0):
    ensure_property_image_table()
    cols = get_property_image_columns()
    if not cols or not cols['property_id'] or not cols['path']:
        return False, 'Image storage is not configured correctly.'

    valid_files = []
    for file_item in image_files:
        if not file_item or not getattr(file_item, 'filename', None):
            continue
        if not file_item.filename.strip():
            continue
        valid_files.append(file_item)

    if not valid_files:
        return True, None

    if existing_count + len(valid_files) > MAX_PROPERTY_IMAGES:
        allowed_more = max(MAX_PROPERTY_IMAGES - existing_count, 0)
        return False, f'You can upload only {allowed_more} more image(s). Maximum is {MAX_PROPERTY_IMAGES} per property.'

    for file_item in valid_files:
        if not is_allowed_image_extension(file_item.filename):
            return False, f'Unsupported file type for {file_item.filename}. Allowed: JPG, JPEG, PNG, WEBP.'
        if not has_valid_image_signature(file_item, file_item.filename):
            return False, f'{file_item.filename} appears corrupted or is not a valid image file.'

    saved_files = []
    use_cloudinary = _cloudinary_property_upload_ready()

    try:
        for file_item in valid_files:

            if use_cloudinary:
                upload_result = cloudinary.uploader.upload(
                    file_item,
                    folder="kayhomes/properties"
                )
                stored_path = upload_result["secure_url"]
            else:
                stored_path = generate_unique_upload_name(file_item.filename)
                destination = os.path.join(app.config['UPLOAD_FOLDER'], stored_path)
                file_item.stream.seek(0)
                file_item.save(destination)

            saved_files.append(stored_path)

            if cols['uploaded_at']:
                insert_stmt = text(f'''
                    INSERT INTO property_image ({cols['property_id']}, {cols['path']}, {cols['uploaded_at']})
                    VALUES (:pid, :img, NOW())
                ''')
            else:
                insert_stmt = text(f'''
                    INSERT INTO property_image ({cols['property_id']}, {cols['path']})
                    VALUES (:pid, :img)
                ''')

            db.session.execute(
                insert_stmt,
                {
                    "pid": property_id,
                    "img": stored_path
                }
            )

        db.session.commit()
        return True, None

    except Exception as e:
        app.logger.exception('Image save failed for property_id=%s', property_id)

        db.session.rollback()

        for saved_path in saved_files:
            if use_cloudinary and str(saved_path).startswith(('http://', 'https://')):
                try:
                    public_id = saved_path.split("/upload/")[1]
                    public_id = public_id.split(".", 1)[0]

                    if "/" in public_id:
                        public_id = public_id.split("/", 1)[1]

                    cloudinary.uploader.destroy(public_id)
                except Exception:
                    pass
            else:
                delete_image_file(saved_path)

        return False, str(e)
        

def delete_image_file(image_path):
    if not image_path:
        return
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], image_path)
    try:
        if os.path.exists(file_path):
            os.remove(file_path)
    except Exception:
        pass


def _format_message_timestamp(value):
    if hasattr(value, 'strftime'):
        return value.strftime('%b %d, %Y %I:%M %p')
    return str(value or '')


def _format_notification_timestamp(value):
    if hasattr(value, 'strftime'):
        return value.strftime('%b %d, %I:%M %p')
    return str(value or '')


def _serialize_message_row(row, current_user_id):
    return {
        'msg_id': row['msg_id'],
        'sender_id': row['sender_id'],
        'receiver_id': row['receiver_id'],
        'message': row['message'],
        'created_at': _format_message_timestamp(row.get('created_at')),
        'is_sender': row['sender_id'] == current_user_id,
    }


def _get_unread_message_count(user_id):
    try:
        return db.session.execute(
            text('SELECT COUNT(*) FROM messages WHERE receiver_id = :uid AND is_read = 0'),
            {'uid': user_id}
        ).scalar() or 0
    except Exception:
        return 0


def _serialize_notification(item):
    return {
        'notification_id': item.notification_id,
        'type': item.type,
        'title': item.title,
        'message': item.message,
        'link': item.link,
        'is_read': bool(item.is_read),
        'created_at_display': _format_notification_timestamp(item.created_at),
    }


def _create_notification(user_id, notification_type, title, message, link=None):
    if not user_id:
        return
    try:
        notification = Notification(
            user_id=user_id,
            type=(notification_type or 'system')[:40],
            title=(title or 'Notification')[:150],
            message=(message or '')[:255],
            link=(link or None),
            is_read=False,
        )
        db.session.add(notification)
        db.session.commit()
    except Exception:
        db.session.rollback()


def _get_unread_notification_count(user_id):
    try:
        return (
            Notification.query
            .filter_by(user_id=user_id, is_read=False)
            .count()
        ) or 0
    except Exception:
        return 0


def _get_latest_notifications(user_id, limit=10):
    try:
        rows = (
            Notification.query
            .filter_by(user_id=user_id)
            .order_by(Notification.created_at.desc(), Notification.notification_id.desc())
            .limit(limit)
            .all()
        )
        return [_serialize_notification(row) for row in rows]
    except Exception:
        return []


def _property_status_column():
    property_columns = get_table_columns('property')
    for candidate in ('status', 'prop_status', 'listing_status'):
        if candidate in property_columns:
            return candidate
    return None


def _normalize_property_status(value):
    token = (value or '').strip().lower()
    return token if token in ALLOWED_PROPERTY_STATUSES else 'available'


def _property_status_presentation(value):
    normalized = _normalize_property_status(value)
    if normalized == 'pending':
        return {
            'value': 'pending',
            'label': 'Pending',
            'badge_class': 'bg-warning-subtle text-warning-emphasis border border-warning-subtle',
        }
    if normalized == 'rented':
        return {
            'value': 'rented',
            'label': 'Rented',
            'badge_class': 'bg-danger-subtle text-danger border border-danger-subtle',
        }
    return {
        'value': 'available',
        'label': 'Available',
        'badge_class': 'bg-success-subtle text-success border border-success-subtle',
    }


def _record_property_view_once(property_id):
    viewed_ids = session.get('viewed_property_ids', [])
    if not isinstance(viewed_ids, list):
        viewed_ids = []

    property_key = int(property_id)
    if property_key in viewed_ids:
        return False

    try:
        db.session.execute(
            text('UPDATE property SET prop_views = COALESCE(prop_views, 0) + 1 WHERE prop_id = :pid'),
            {'pid': property_key}
        )
        owner_id = db.session.execute(
            text('SELECT prop_userid FROM property WHERE prop_id = :pid LIMIT 1'),
            {'pid': property_key}
        ).scalar()
        if owner_id:
            db.session.execute(
                text(
                    '''
                    INSERT INTO property_view_events (property_id, owner_id, viewer_id, viewed_at)
                    VALUES (:pid, :owner_id, :viewer_id, NOW())
                    '''
                ),
                {
                    'pid': property_key,
                    'owner_id': owner_id,
                    'viewer_id': session.get('user_id'),
                }
            )
        db.session.commit()
        viewed_ids.append(property_key)
        session['viewed_property_ids'] = viewed_ids
        session.modified = True
        return True
    except Exception:
        db.session.rollback()
        return False


def _normalize_nigerian_phone_number(phone_number):
    sanitized = re.sub(r'[\s\-()]', '', (phone_number or '').strip())
    sanitized = sanitized.lstrip('+')
    if not sanitized:
        return None
    if sanitized.startswith('0'):
        sanitized = '234' + sanitized[1:]
    return sanitized if sanitized.isdigit() and len(sanitized) >= 10 else None


def _build_quick_contact_links(phone_number, property_title, page_url):
    normalized_phone = _normalize_nigerian_phone_number(phone_number)
    if not normalized_phone:
        return {}

    message = f"Hello, I’m interested in your property: {property_title} on KayHomes. {page_url}"
    encoded_message = quote_plus(message)
    return {
        'whatsapp_url': f'https://wa.me/{normalized_phone}?text={encoded_message}',
        'call_url': f'tel:+{normalized_phone}',
    }


def _saved_search_filter_params(saved_search):
    params = {}
    for field in ('q', 'state', 'lga', 'property_type', 'furnished', 'sort'):
        value = getattr(saved_search, field, None)
        if value:
            params[field] = value

    for field in ('bedrooms', 'bathrooms', 'min_price', 'max_price'):
        value = getattr(saved_search, field, None)
        if value is not None:
            params[field] = value

    return params


def _properties_query_params(filters):
    params = {}
    for field in ('q', 'status', 'state', 'lga', 'property_type', 'sort'):
        value = filters.get(field)
        if value:
            params[field] = value

    for field in ('bedrooms', 'bathrooms', 'min_price', 'max_price'):
        value = filters.get(field)
        if value is not None:
            params[field] = value

    furnished_value = filters.get('furnished_raw')
    if furnished_value:
        params['furnished'] = furnished_value

    return params


def _serialize_saved_search(saved_search):
    params = _saved_search_filter_params(saved_search)
    created_at = getattr(saved_search, 'created_at', None)
    created_at_display = created_at.strftime('%b %d, %Y') if created_at else 'Recently'

    return {
        'search_id': saved_search.search_id,
        'name': saved_search.name,
        'q': saved_search.q,
        'state': saved_search.state,
        'lga': saved_search.lga,
        'property_type': saved_search.property_type,
        'bedrooms': saved_search.bedrooms,
        'bathrooms': saved_search.bathrooms,
        'min_price': saved_search.min_price,
        'max_price': saved_search.max_price,
        'furnished': saved_search.furnished,
        'sort': saved_search.sort,
        'created_at_display': created_at_display,
        'run_url': url_for('run_saved_search', search_id=saved_search.search_id),
        'delete_url': url_for('delete_saved_search', search_id=saved_search.search_id),
        'properties_url': url_for('properties', **params),
        'query_summary': _build_saved_search_summary(saved_search),
    }


def _build_saved_search_summary(saved_search):
    summary = []
    if saved_search.q:
        summary.append(f"Search: {saved_search.q}")
    if saved_search.state:
        summary.append(f"State: {saved_search.state}")
    if saved_search.lga:
        summary.append(f"LGA: {saved_search.lga}")
    if saved_search.property_type:
        summary.append(f"Type: {saved_search.property_type}")
    if saved_search.bedrooms is not None:
        summary.append(f"Bedrooms: {saved_search.bedrooms}+")
    if saved_search.bathrooms is not None:
        summary.append(f"Bathrooms: {saved_search.bathrooms}+")
    if saved_search.min_price is not None:
        summary.append(f"Min: {format_naira(saved_search.min_price)}")
    if saved_search.max_price is not None:
        summary.append(f"Max: {format_naira(saved_search.max_price)}")
    if saved_search.furnished:
        summary.append(f"Furnished: {saved_search.furnished.capitalize()}")
    if saved_search.sort:
        summary.append(f"Sort: {saved_search.sort.replace('_', ' ').title()}")
    return summary


def _get_profile_stats(user_id):
    total_properties = 0
    try:
        total_properties = Property.query.filter_by(prop_userid=user_id).count() or 0
    except Exception:
        total_properties = 0

    status_column = _property_status_column()
    property_columns = get_table_columns('property')

    if status_column and status_column in property_columns and hasattr(Property, status_column):
        try:
            if status_column == 'prop_status':
                active_filter = func.lower(func.coalesce(getattr(Property, status_column), 'available')) == 'available'
            else:
                active_filter = getattr(Property, status_column) == 'Active'
            active_listings = (
                Property.query
                .filter(Property.prop_userid == user_id, active_filter)
                .count()
                or 0
            )
        except Exception:
            active_listings = total_properties
    else:
        active_listings = total_properties

    try:
        favorites_count = Favorite.query.filter_by(fav_userid=user_id).count() or 0
    except Exception:
        favorites_count = 0

    unread_messages = _get_unread_message_count(user_id)

    views_count = None
    for candidate in ('view_count', 'views', 'prop_views'):
        if candidate in property_columns:
            try:
                views_count = db.session.execute(
                    text(f'SELECT COALESCE(SUM({candidate}), 0) FROM property WHERE prop_userid = :uid'),
                    {'uid': user_id}
                ).scalar() or 0
            except Exception:
                views_count = 0
            break

    return {
        'total_properties': total_properties,
        'active_listings': active_listings,
        'favorites_count': favorites_count,
        'unread_messages': unread_messages,
        'views_count': views_count,
    }


def _last_7_days_labels():
    end_date = datetime.now().date()
    days = [end_date - timedelta(days=offset) for offset in range(6, -1, -1)]
    return days, [day.strftime('%a') for day in days]


def _rows_to_daily_series(rows, day_list):
    by_day = {}
    for row in rows:
        day_value = row.get('day')
        if hasattr(day_value, 'date'):
            key = day_value.date()
        else:
            try:
                key = datetime.strptime(str(day_value), '%Y-%m-%d').date()
            except Exception:
                continue
        by_day[key] = int(row.get('total') or 0)
    return [by_day.get(day, 0) for day in day_list]


def _get_analytics_payload(user_id):
    day_list, day_labels = _last_7_days_labels()
    start_date = day_list[0]

    metrics = {
        'total_views': 0,
        'total_inquiries': 0,
        'total_favorites': 0,
        'total_reviews': 0,
        'active_listings': 0,
        'rented_listings': 0,
    }

    try:
        listing_counts = db.session.execute(
            text(
                '''
                SELECT
                    COALESCE(SUM(CASE WHEN LOWER(COALESCE(prop_status, 'available')) = 'available' THEN 1 ELSE 0 END), 0) AS active_listings,
                    COALESCE(SUM(CASE WHEN LOWER(COALESCE(prop_status, 'available')) = 'rented' THEN 1 ELSE 0 END), 0) AS rented_listings,
                    COALESCE(SUM(COALESCE(prop_views, 0)), 0) AS total_views
                FROM property
                WHERE prop_userid = :uid
                '''
            ),
            {'uid': user_id}
        ).mappings().first() or {}
        metrics['active_listings'] = int(listing_counts.get('active_listings') or 0)
        metrics['rented_listings'] = int(listing_counts.get('rented_listings') or 0)
        metrics['total_views'] = int(listing_counts.get('total_views') or 0)
    except Exception:
        pass

    try:
        metrics['total_inquiries'] = int(
            db.session.execute(
                text(
                    '''
                    SELECT COUNT(*)
                    FROM messages m
                    JOIN property p ON p.prop_id = m.property_id
                    WHERE p.prop_userid = :uid
                      AND m.sender_id != :uid
                    '''
                ),
                {'uid': user_id}
            ).scalar() or 0
        )
    except Exception:
        pass

    try:
        metrics['total_favorites'] = int(
            db.session.execute(
                text(
                    '''
                    SELECT COUNT(*)
                    FROM favorites f
                    JOIN property p ON p.prop_id = f.fav_propid
                    WHERE p.prop_userid = :uid
                    '''
                ),
                {'uid': user_id}
            ).scalar() or 0
        )
    except Exception:
        pass

    try:
        metrics['total_reviews'] = int(
            db.session.execute(
                text(
                    '''
                    SELECT COUNT(*)
                    FROM property_reviews r
                    WHERE r.owner_id = :uid
                    '''
                ),
                {'uid': user_id}
            ).scalar() or 0
        )
    except Exception:
        pass

    try:
        top_property = db.session.execute(
            text(
                '''
                SELECT prop_id, prop_title, COALESCE(prop_views, 0) AS views
                FROM property
                WHERE prop_userid = :uid
                ORDER BY COALESCE(prop_views, 0) DESC, prop_id DESC
                LIMIT 1
                '''
            ),
            {'uid': user_id}
        ).mappings().first()
    except Exception:
        top_property = None

    try:
        daily_views_rows = db.session.execute(
            text(
                '''
                SELECT DATE(viewed_at) AS day, COUNT(*) AS total
                FROM property_view_events
                WHERE owner_id = :uid
                  AND viewed_at >= :start_date
                GROUP BY DATE(viewed_at)
                ORDER BY DATE(viewed_at) ASC
                '''
            ),
            {'uid': user_id, 'start_date': start_date}
        ).mappings().all()
    except Exception:
        daily_views_rows = []

    try:
        daily_inquiries_rows = db.session.execute(
            text(
                '''
                SELECT DATE(m.created_at) AS day, COUNT(*) AS total
                FROM messages m
                JOIN property p ON p.prop_id = m.property_id
                WHERE p.prop_userid = :uid
                  AND m.sender_id != :uid
                  AND m.created_at >= :start_date
                GROUP BY DATE(m.created_at)
                ORDER BY DATE(m.created_at) ASC
                '''
            ),
            {'uid': user_id, 'start_date': start_date}
        ).mappings().all()
    except Exception:
        daily_inquiries_rows = []

    try:
        daily_favorites_rows = db.session.execute(
            text(
                '''
                SELECT DATE(f.created_at) AS day, COUNT(*) AS total
                FROM favorites f
                JOIN property p ON p.prop_id = f.fav_propid
                WHERE p.prop_userid = :uid
                  AND f.created_at >= :start_date
                GROUP BY DATE(f.created_at)
                ORDER BY DATE(f.created_at) ASC
                '''
            ),
            {'uid': user_id, 'start_date': start_date}
        ).mappings().all()
    except Exception:
        daily_favorites_rows = []

    chart_data = {
        'labels': day_labels,
        'daily_views': _rows_to_daily_series(daily_views_rows, day_list),
        'daily_inquiries': _rows_to_daily_series(daily_inquiries_rows, day_list),
        'daily_favorites': _rows_to_daily_series(daily_favorites_rows, day_list),
    }

    return {
        'metrics': metrics,
        'chart_data': chart_data,
        'top_property': {
            'prop_id': top_property.get('prop_id'),
            'prop_title': top_property.get('prop_title'),
            'views': int(top_property.get('views') or 0),
            'detail_url': url_for('property_detail', property_id=top_property.get('prop_id')),
        } if top_property else None,
    }


def _format_plain_price(value):
    if value is None:
        return ''
    return str(value)


def _format_currency_price(value):
    return format_naira(value)


def _placeholder_property_image_url():
    return url_for('static', filename='images/img1.jpg')


def _upload_image_url(image_name):
    if not image_name:
        return _placeholder_property_image_url()

    # If it's already a Cloudinary URL, return it directly
    if image_name.startswith("http://") or image_name.startswith("https://"):
        return image_name

    # Otherwise, treat it as a local upload
    return url_for(
        "static",
        filename=f"uploads/{image_name}"
    )


def _default_avatar_url():
    return url_for('static', filename='default-avatar.png')


def _user_avatar_url(user_avatar):
    avatar_name = (user_avatar or '').strip()
    if not avatar_name:
        return _default_avatar_url()
    avatar_path = os.path.join(_avatar_upload_dir(), os.path.basename(avatar_name))
    if not os.path.isfile(avatar_path):
        return _default_avatar_url()
    return url_for('static', filename=f'uploads/avatars/{avatar_name}')


def _avatar_upload_dir():
    base_dir = app.config.get('AVATAR_UPLOAD_FOLDER')
    if not base_dir:
        base_dir = os.path.join(app.root_path, 'static', 'uploads', 'avatars')
    os.makedirs(base_dir, exist_ok=True)
    return os.path.abspath(base_dir)


def _avatar_has_valid_signature(file_obj, extension):
    try:
        head = file_obj.read(32)
        file_obj.seek(0)
    except Exception:
        return False

    if extension in {'.jpg', '.jpeg'}:
        return head.startswith(b'\xff\xd8\xff')
    if extension == '.png':
        return head.startswith(b'\x89PNG\r\n\x1a\n')
    if extension == '.webp':
        return len(head) >= 12 and head[0:4] == b'RIFF' and head[8:12] == b'WEBP'
    return False


def _avatar_file_size_ok(file_obj):
    try:
        current_pos = file_obj.tell()
        file_obj.seek(0, os.SEEK_END)
        file_size = file_obj.tell()
        file_obj.seek(current_pos)
        return file_size <= MAX_AVATAR_FILE_SIZE
    except Exception:
        return False


def _delete_avatar_file(filename):
    safe_name = os.path.basename((filename or '').strip())
    if not safe_name:
        return

    avatar_dir = _avatar_upload_dir()
    target_path = os.path.abspath(os.path.join(avatar_dir, safe_name))
    if not target_path.startswith(avatar_dir + os.sep):
        return

    try:
        if os.path.isfile(target_path):
            os.remove(target_path)
    except Exception:
        app.logger.warning('Failed to delete avatar file: %s', target_path)


def _save_user_avatar(uploaded_file):
    if not uploaded_file or not uploaded_file.filename:
        return None, 'Please choose an image to upload.'

    original_name = secure_filename(uploaded_file.filename)
    _, ext = os.path.splitext(original_name)
    ext = ext.lower()

    if ext not in ALLOWED_AVATAR_EXTENSIONS:
        return None, 'Profile photo must be an image and not exceed 10MB.'

    if not _avatar_file_size_ok(uploaded_file.stream):
        return None, 'Profile photo must be an image and not exceed 10MB.'

    if not _avatar_has_valid_signature(uploaded_file.stream, ext):
        return None, 'Profile photo must be an image and not exceed 10MB.'

    avatar_dir = _avatar_upload_dir()
    unique_name = f"avatar_{secrets.token_hex(12)}{ext}"
    target_path = os.path.abspath(os.path.join(avatar_dir, unique_name))
    if not target_path.startswith(avatar_dir + os.sep):
        return None, 'Invalid upload destination.'

    uploaded_file.stream.seek(0)
    uploaded_file.save(target_path)
    return unique_name, None


def _format_property_room_value(value):
    if value in (None, ''):
        return 'N/A'
    try:
        numeric_value = int(float(value))
        return str(numeric_value)
    except (TypeError, ValueError):
        return str(value)


def _serialize_property_model(property_obj, cover_image=None):
    if cover_image is None:
        image_rows = get_property_images(property_obj.prop_id)
        cover_image = image_rows[0]['image_path'] if image_rows else None
    room_vals = _property_room_values(property_obj)
    status_meta = _property_status_presentation(getattr(property_obj, 'prop_status', None))
    return {
        'prop_id': property_obj.prop_id,
        'prop_title': property_obj.prop_title,
        'prop_type': property_obj.category.cat_name if getattr(property_obj, 'category', None) else property_obj.prop_type,
        'listing_type': property_obj.listing_type,
        'prop_status': status_meta['value'],
        'status_label': status_meta['label'],
        'status_badge_class': status_meta['badge_class'],
        'prop_views': int(getattr(property_obj, 'prop_views', 0) or 0),
        'prop_desc': property_obj.prop_desc,
        'prop_price': _format_plain_price(property_obj.prop_price),
        'prop_price_display': _format_currency_price(property_obj.prop_price),
        'prop_location': property_obj.prop_location,
        'prop_state': property_obj.prop_state,
        'prop_address': property_obj.prop_address,
        'prop_bedroom': room_vals['bedrooms'],
        'prop_bathroom': room_vals['bathrooms'],
        'prop_toilet': room_vals['toilets'],
        'prop_area': room_vals['area'],
        'area_sqm': room_vals['area'],
        'prop_area_unit': room_vals['area_unit'],
        'bedrooms': _format_property_room_value(room_vals['bedrooms']),
        'bathrooms': _format_property_room_value(room_vals['bathrooms']),
        'toilets': _format_property_room_value(room_vals['toilets']),
        'area': _format_property_room_value(room_vals['area']),
        'area_unit': room_vals['area_unit'],
        'short_desc': (property_obj.prop_desc or '')[:160],
        'cover_image': cover_image,
        'cover_image_url': _upload_image_url(cover_image),
        'detail_url': url_for('property_detail', property_id=property_obj.prop_id),
    }


def _get_inquiry_count_map(property_ids):
    if not property_ids:
        return {}

    try:
        rows = db.session.execute(
            text(
                'SELECT inqu_propid, COUNT(*) AS total '
                'FROM inquiries '
                'WHERE inqu_propid IN :prop_ids '
                'GROUP BY inqu_propid'
            ).bindparams(prop_ids=tuple(property_ids), expanding=True)
        ).mappings().all()
    except Exception:
        rows = []

    return {row['inqu_propid']: int(row['total'] or 0) for row in rows if row.get('inqu_propid')}


def _get_property_review_stats(property_id):
    try:
        row = db.session.execute(
            text('''
                SELECT 
                    COUNT(*) AS total_count,
                    COALESCE(AVG(rating), 0.0) AS avg_rating,
                    COALESCE(SUM(CASE WHEN rating = 5 THEN 1 ELSE 0 END), 0) AS count_5,
                    COALESCE(SUM(CASE WHEN rating = 4 THEN 1 ELSE 0 END), 0) AS count_4,
                    COALESCE(SUM(CASE WHEN rating = 3 THEN 1 ELSE 0 END), 0) AS count_3,
                    COALESCE(SUM(CASE WHEN rating = 2 THEN 1 ELSE 0 END), 0) AS count_2,
                    COALESCE(SUM(CASE WHEN rating = 1 THEN 1 ELSE 0 END), 0) AS count_1
                FROM property_reviews
                WHERE property_id = :pid AND is_visible = 1
            '''),
            {'pid': property_id}
        ).mappings().first()
    except Exception:
        row = None

    if not row or not row.get('total_count'):
        return {
            'avg_rating': 0.0,
            'avg_rating_display': '0.0',
            'review_count': 0,
            'star_counts': {5: 0, 4: 0, 3: 0, 2: 0, 1: 0},
            'star_percentages': {5: 0, 4: 0, 3: 0, 2: 0, 1: 0},
        }

    total = int(row['total_count'] or 0)
    avg_val = round(float(row['avg_rating'] or 0.0), 1)

    star_counts = {
        5: int(row['count_5'] or 0),
        4: int(row['count_4'] or 0),
        3: int(row['count_3'] or 0),
        2: int(row['count_2'] or 0),
        1: int(row['count_1'] or 0),
    }

    star_percentages = {
        star: round((count / total * 100)) if total > 0 else 0
        for star, count in star_counts.items()
    }

    return {
        'avg_rating': avg_val,
        'avg_rating_display': f"{avg_val:.1f}",
        'review_count': total,
        'star_counts': star_counts,
        'star_percentages': star_percentages,
    }


def _get_owner_review_stats(owner_id):
    try:
        row = db.session.execute(
            text('''
                SELECT 
                    COUNT(*) AS total_count,
                    COALESCE(AVG(rating), 0.0) AS avg_rating
                FROM property_reviews
                WHERE owner_id = :oid AND is_visible = 1
            '''),
            {'oid': owner_id}
        ).mappings().first()
    except Exception:
        row = None

    if not row or not row.get('total_count'):
        return {
            'avg_rating': 0.0,
            'avg_rating_display': '0.0',
            'review_count': 0,
        }

    total = int(row['total_count'] or 0)
    avg_val = round(float(row['avg_rating'] or 0.0), 1)

    return {
        'avg_rating': avg_val,
        'avg_rating_display': f"{avg_val:.1f}",
        'review_count': total,
    }


def _get_property_reviews(property_id, limit=None):
    limit_clause = f" LIMIT {int(limit)}" if limit else ""
    try:
        query_str = f'''
            SELECT 
                r.review_id,
                r.property_id,
                r.reviewer_id,
                r.owner_id,
                r.rating,
                r.review_text,
                r.review_tags,
                r.created_at,
                u.user_fname,
                u.user_lname
            FROM property_reviews r
            JOIN users u ON r.reviewer_id = u.user_id
            WHERE r.property_id = :pid AND r.is_visible = 1
            ORDER BY r.created_at DESC, r.review_id DESC
            {limit_clause}
        '''
        rows = db.session.execute(text(query_str), {'pid': property_id}).mappings().all()
    except Exception:
        rows = []


    reviews = []
    for row in rows:
        r_dict = dict(row)
        tags_raw = r_dict.get('review_tags') or ''
        tags_list = [t.strip() for t in tags_raw.split(',') if t.strip()] if tags_raw else []

        created_at = r_dict.get('created_at')
        if hasattr(created_at, 'strftime'):
            date_str = created_at.strftime('%b %d, %Y')
        else:
            date_str = str(created_at)

        fname = r_dict.get('user_fname') or 'User'
        lname = r_dict.get('user_lname') or ''
        reviewer_name = f"{fname} {lname}".strip()
        initial = fname[0].upper() if fname else 'U'

        reviews.append({
            'review_id': r_dict['review_id'],
            'rating': r_dict['rating'],
            'review_text': r_dict.get('review_text') or '',
            'tags': tags_list,
            'date_display': date_str,
            'reviewer_name': reviewer_name,
            'reviewer_initial': initial,
        })

    return reviews


def _serialize_listing_row(row, inquiry_count=0, cover_image=None):
    listing_id = row['prop_id']
    if cover_image is None:
        image_rows = get_property_images(listing_id)
        cover_image = image_rows[0]['image_path'] if image_rows else None
    created_at = row.get('prop_regdate')
    room_vals = _property_room_values(row)
    status_meta = _property_status_presentation(row.get('prop_status'))
    return {
        'prop_id': listing_id,
        'prop_title': row['prop_title'],
        'prop_price': row['prop_price'],
        'prop_price_display': _format_currency_price(row['prop_price']),
        'prop_location': row['prop_location'],
        'prop_type': row['prop_type'],
        'listing_type': row['listing_type'],
        'prop_status': status_meta['value'],
        'status_label': status_meta['label'],
        'status_badge_class': status_meta['badge_class'],
        'prop_views': int(row.get('prop_views') or 0),
        'prop_userid': row['prop_userid'],
        'prop_desc': row['prop_desc'],
        'prop_state': row['prop_state'],
        'prop_address': row['prop_address'],
        'prop_bedroom': room_vals['bedrooms'],
        'prop_bathroom': room_vals['bathrooms'],
        'prop_toilet': room_vals['toilets'],
        'prop_area': room_vals['area'],
        'area_sqm': room_vals['area'],
        'prop_area_unit': room_vals['area_unit'],
        'bedrooms': _format_property_room_value(room_vals['bedrooms']),
        'bathrooms': _format_property_room_value(room_vals['bathrooms']),
        'toilets': _format_property_room_value(room_vals['toilets']),
        'area': _format_property_room_value(room_vals['area']),
        'area_unit': room_vals['area_unit'],
        'image': cover_image,
        'image_url': _upload_image_url(cover_image),
        'created_at': created_at.strftime('%b %d, %Y') if hasattr(created_at, 'strftime') else 'Recently posted',
        'inquiry_count': inquiry_count,
        'view_url': url_for('property_detail', property_id=listing_id),
        'edit_url': url_for('edit_listing', property_id=listing_id),
        'delete_url': url_for('delete_listing', property_id=listing_id),
        'status_update_url': url_for('update_listing_status', property_id=listing_id),
    }

def _build_my_listings_payload(user_id, page=None, per_page=None):
    columns = get_table_columns('property')
    select_cols = [
        'prop_id',
        'prop_title',
        'prop_price',
        'prop_location',
        'prop_type',
        'listing_type',
        'prop_status',
        'prop_views',
        'prop_userid',
        'prop_desc',
        'prop_state',
        'prop_address',
    ]

    if 'prop_status' not in columns and 'prop_status' in select_cols:
        select_cols.remove('prop_status')
    if 'prop_views' not in columns and 'prop_views' in select_cols:
        select_cols.remove('prop_views')

    for col in ('prop_regdate', 'prop_bedroom', 'prop_bathroom', 'prop_toilet', 'prop_area', 'prop_area_unit', 'bedrooms', 'bathrooms', 'toilets', 'area_sqm'):
        if col in columns:
            select_cols.append(col)

    count_query = text('SELECT COUNT(*) FROM property WHERE prop_userid = :uid')

    page = _coerce_page(page, default=1)
    per_page = int(per_page or 12)
    total = 0
    try:
        total = int(db.session.execute(count_query, {'uid': user_id}).scalar() or 0)
    except Exception:
        total = 0

    pagination = _build_pagination(total, page, per_page)
    offset = (pagination.page - 1) * pagination.per_page

    query = text(f'''
        SELECT {', '.join(select_cols)}
        FROM property
        WHERE prop_userid = :uid
        ORDER BY prop_id DESC
        LIMIT :limit OFFSET :offset
    ''')

    try:
        rows = db.session.execute(query, {'uid': user_id, 'limit': pagination.per_page, 'offset': offset}).mappings().all()
    except Exception:
        rows = []

    property_ids = [row['prop_id'] for row in rows]
    inquiry_map = _get_inquiry_count_map(property_ids)
    cover_map = get_bulk_cover_images(property_ids)

    return {
        'listings': [
            _serialize_listing_row(row, inquiry_count=inquiry_map.get(row['prop_id'], 0), cover_image=cover_map.get(row['prop_id']))
            for row in rows
        ],
        'pagination': pagination,
    }


def _build_favorites_payload(user_id, page=None, per_page=None):
    page = _coerce_page(page, default=1)
    per_page = int(per_page or 12)

    try:
        total = int(
            db.session.execute(
                text('SELECT COUNT(*) FROM favorites WHERE fav_userid = :uid'),
                {'uid': user_id}
            ).scalar() or 0
        )
    except Exception:
        total = 0

    pagination = _build_pagination(total, page, per_page)
    offset = (pagination.page - 1) * pagination.per_page

    try:
        rows = db.session.execute(
            text(
                '''
                SELECT p.*, u.user_fname, u.user_lname
                FROM favorites f
                JOIN property p ON p.prop_id = f.fav_propid
                JOIN users u ON u.user_id = p.prop_userid
                WHERE f.fav_userid = :uid
                ORDER BY f.fav_id DESC
                LIMIT :limit OFFSET :offset
                '''
            ),
            {'uid': user_id, 'limit': pagination.per_page, 'offset': offset}
        ).mappings().all()
    except Exception:
        rows = []

    property_ids = [row['prop_id'] for row in rows]
    cover_map = get_bulk_cover_images(property_ids)
    favorites = []
    for row in rows:
        cover_image = cover_map.get(row['prop_id'])
        favorites.append({
            'prop_id': row['prop_id'],
            'prop_title': row['prop_title'],
            'prop_price': row['prop_price'],
            'prop_location': row['prop_location'],
            'prop_type': row['prop_type'],
            'listing_type': row['listing_type'],
            'owner_name': f"{row['user_fname']} {row['user_lname']}",
            'image': cover_image,
            'image_url': _upload_image_url(cover_image),
        })

    return {
        'favorites': favorites,
        'pagination': pagination,
    }


def _build_property_detail_payload(property_id, current_user_id):
    ensure_property_image_table()
    ensure_property_specs_schema()

    try:
        row = db.session.execute(
            text(
                '''
                SELECT p.*
                FROM property p
                WHERE p.prop_id = :pid
                LIMIT 1
                '''
            ),
            {'pid': property_id}
        ).mappings().first()
    except Exception:
        row = None

    if not row:
        return None

    property_data = dict(row)
    room_values = _property_room_values(property_data)
    property_data['bedrooms'] = room_values['bedrooms']
    property_data['bathrooms'] = room_values['bathrooms']
    property_data['toilets'] = room_values['toilets']
    property_data['area_sqm'] = room_values['area']

    image_rows = get_property_images(property_id)
    image_paths = [image_row.get('image_path') for image_row in image_rows if image_row.get('image_path')]
    cover_image = image_paths[0] if image_paths else None

    is_favorite = False
    if current_user_id:
        try:
            is_favorite = bool(
                db.session.execute(
                    text('SELECT 1 FROM favorites WHERE fav_userid = :uid AND fav_propid = :pid LIMIT 1'),
                    {'uid': current_user_id, 'pid': property_id}
                ).scalar()
            )
        except Exception:
            is_favorite = False

    return {
        'prop_id': property_data.get('prop_id'),
        'prop_title': property_data.get('prop_title'),
        'prop_price': property_data.get('prop_price'),
        'prop_price_display': _format_currency_price(property_data.get('prop_price')),
        'prop_location': property_data.get('prop_location'),
        'prop_state': property_data.get('prop_state'),
        'prop_lga': property_data.get('prop_lga'),
        'prop_address': property_data.get('prop_address'),
        'prop_desc': property_data.get('prop_desc'),
        'prop_type': property_data.get('prop_type'),
        'listing_type': property_data.get('listing_type'),
        'prop_status': _normalize_property_status(property_data.get('prop_status')),
        'prop_views': int(property_data.get('prop_views') or 0),
        'bedrooms': room_values['bedrooms'],
        'bathrooms': room_values['bathrooms'],
        'toilets': room_values['toilets'],
        'area_sqm': room_values['area'],
        'prop_area_unit': room_values['area_unit'],
        'cover_image_url': _upload_image_url(cover_image),
        'gallery_images': [
            {
                'image_path': image_path,
                'image_url': _upload_image_url(image_path),
            }
            for image_path in image_paths[1:]
        ],
        'images': image_paths,
        'is_favorite': is_favorite,
    }


def _property_room_values(property_row):
    bedroom_candidates = ('bedrooms', 'prop_bedroom', 'prop_bedrooms', 'bedroom', 'prop_beds', 'beds', 'prop_bed')
    bathroom_candidates = ('bathrooms', 'prop_bathroom', 'prop_bathrooms', 'bathroom', 'prop_baths', 'baths', 'prop_bath')
    toilet_candidates = ('toilets', 'prop_toilet', 'prop_toilets', 'toilet')
    area_candidates = ('area_sqm', 'prop_area', 'area')

    def pick_value(candidates):
        for candidate in candidates:
            if isinstance(property_row, dict):
                if candidate in property_row and property_row.get(candidate) not in (None, ''):
                    return property_row.get(candidate)
            else:
                if hasattr(property_row, candidate) and getattr(property_row, candidate) not in (None, ''):
                    return getattr(property_row, candidate)
        return None

    bedrooms = pick_value(bedroom_candidates)
    bathrooms = pick_value(bathroom_candidates)
    toilets = pick_value(toilet_candidates)
    area = pick_value(area_candidates)

    area_unit = None
    if isinstance(property_row, dict):
        area_unit = property_row.get('prop_area_unit') or 'sqm'
    else:
        area_unit = getattr(property_row, 'prop_area_unit', 'sqm') or 'sqm'

    return {
        'bedrooms': bedrooms,
        'bathrooms': bathrooms,
        'toilets': toilets,
        'area': area,
        'area_unit': area_unit,
    }

def get_bulk_cover_images(property_ids):
    if not property_ids:
        return {}

    pimg_cols = get_property_image_columns()
    fk_col = 'property_id' if 'property_id' in pimg_cols else 'pimg_propid'
    path_col = 'image_path' if 'image_path' in pimg_cols else 'pimg_url'
    id_col = 'image_id' if 'image_id' in pimg_cols else 'pimg_id'

    try:
        rows = db.session.execute(
            text(f'''
                SELECT {fk_col} AS pid, {path_col} AS img_path
                FROM property_image
                WHERE {fk_col} IN :pids
                ORDER BY {id_col} ASC
            ''').bindparams(pids=tuple(property_ids), expanding=True)
        ).mappings().all()
    except Exception:
        rows = []

    cover_map = {}
    for row in rows:
        pid = row['pid']
        if pid not in cover_map:
            cover_map[pid] = row['img_path']

    return cover_map


def _serialize_similar_property(property_row, cover_image=None):
    property_id = property_row.get('prop_id')
    if cover_image is None and property_id:
        image_rows = get_property_images(property_id)
        cover_image = image_rows[0]['image_path'] if image_rows else None

    room_values = _property_room_values(property_row)

    return {
        'prop_id': property_id,
        'prop_title': property_row.get('prop_title') or '',
        'prop_price_display': _format_currency_price(property_row.get('prop_price')),
        'prop_location': property_row.get('prop_location') or '',
        'prop_state': property_row.get('prop_state') or '',
        'cover_image_url': _upload_image_url(cover_image),
        'detail_url': url_for('property_detail', property_id=property_id),
        'bedrooms': _format_property_room_value(room_values['bedrooms']),
        'bathrooms': _format_property_room_value(room_values['bathrooms']),
        'toilets': _format_property_room_value(room_values['toilets']),
        'area': _format_property_room_value(room_values['area']),
        'area_unit': room_values['area_unit'],
    }


def _text_or_empty(value):
    if value is None:
        return ''
    return str(value).strip()


def _normalize_lookup(value):
    return _text_or_empty(value).lower()


def _get_state_lga_form_data():
    states = []
    state_lga_map = {}

    try:
        state_rows = db.session.execute(
            text('SELECT state_id, state_name FROM state ORDER BY state_name ASC')
        ).mappings().all()
        states = [dict(row) for row in state_rows]
    except Exception:
        states = []

    try:
        lga_rows = db.session.execute(
            text(
                '''SELECT s.state_name, l.lga_name
                   FROM lga l
                   JOIN state s ON s.state_id = l.lga_stateid
                   ORDER BY s.state_name ASC, l.lga_name ASC'''
            )
        ).mappings().all()
        for row in lga_rows:
            state_name = (row.get('state_name') or '').strip()
            lga_name = (row.get('lga_name') or '').strip()
            if not state_name or not lga_name:
                continue
            state_lga_map.setdefault(state_name, []).append(lga_name)
    except Exception:
        state_lga_map = {}

    return states, state_lga_map


def _parse_int_filter(value):
    raw = (value or '').strip()
    if not raw:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parse_price_filter(value):
    raw = (value or '').strip()
    if not raw:
        return None
    normalized = raw.replace('₦', '').replace(',', '').strip()
    try:
        return float(normalized)
    except (TypeError, ValueError):
        return None


def _parse_bool_filter(value):
    token = (value or '').strip().lower()
    if token in {'1', 'true', 'yes', 'y'}:
        return True
    if token in {'0', 'false', 'no', 'n'}:
        return False
    return None


def _extract_property_filters(args):
    sort_value = (args.get('sort') or 'newest').strip().lower()
    if sort_value not in {'newest', 'oldest', 'price_low', 'price_high'}:
        sort_value = 'newest'

    return {
        'q': (args.get('q') or '').strip(),
        'status': _normalize_property_status(args.get('status')) if (args.get('status') or '').strip() else '',
        'state': (args.get('state') or '').strip(),
        'lga': (args.get('lga') or '').strip(),
        'property_type': (args.get('property_type') or '').strip(),
        'bedrooms': _parse_int_filter(args.get('bedrooms')),
        'bathrooms': _parse_int_filter(args.get('bathrooms')),
        'min_price': _parse_price_filter(args.get('min_price')),
        'max_price': _parse_price_filter(args.get('max_price')),
        'furnished_raw': (args.get('furnished') or '').strip(),
        'furnished': _parse_bool_filter(args.get('furnished')),
        'sort': sort_value,
    }


def _build_property_price_expr():
    cleaned_price = func.replace(func.replace(func.trim(Property.prop_price), '₦', ''), ',', '')
    return cast(func.nullif(cleaned_price, ''), Float)


def _apply_properties_filters(query, filters, selected_category_obj=None):
    if selected_category_obj:
        query = query.filter(Property.category_id == selected_category_obj.cat_id)

    keyword = filters.get('q') or ''
    if keyword:
        like_pattern = f"%{keyword}%"
        query = query.filter(
            or_(
                Property.prop_title.ilike(like_pattern),
                Property.prop_location.ilike(like_pattern),
                Property.prop_state.ilike(like_pattern),
                Property.prop_lga.ilike(like_pattern),
            )
        )

    status_value = (filters.get('status') or '').strip().lower()
    if status_value:
        query = query.filter(func.lower(func.coalesce(Property.prop_status, 'available')) == status_value)
    else:
        query = query.filter(
            (Property.prop_status == 'available') |
            (Property.prop_status.is_(None))
        )

    state_value = filters.get('state') or ''
    if state_value:
        query = query.filter(Property.prop_state.ilike(f"%{state_value}%"))

    lga_value = filters.get('lga') or ''
    if lga_value:
        query = query.filter(Property.prop_lga.ilike(f"%{lga_value}%"))

    property_type = filters.get('property_type') or ''
    if property_type:
        query = query.outerjoin(Category, Property.category_id == Category.cat_id)
        query = query.filter(
            or_(
                Property.prop_type.ilike(property_type),
                Category.cat_name.ilike(property_type),
            )
        )

    bedrooms = filters.get('bedrooms')
    if bedrooms is not None:
        query = query.filter(
            or_(
                Property.bedrooms == bedrooms,
                Property.prop_bedroom == bedrooms,
            )
        )

    bathrooms = filters.get('bathrooms')
    if bathrooms is not None:
        query = query.filter(
            or_(
                Property.bathrooms == bathrooms,
                Property.prop_bathroom == bathrooms,
            )
        )

    furnished = filters.get('furnished')
    if furnished is not None:
        if hasattr(Property, 'furnished'):
            query = query.filter(getattr(Property, 'furnished').is_(furnished))
        elif hasattr(Property, 'is_furnished'):
            query = query.filter(getattr(Property, 'is_furnished').is_(furnished))
        else:
            furnished_pattern = '%furnished%'
            if furnished:
                query = query.filter(
                    or_(
                        Property.prop_desc.ilike(furnished_pattern),
                        Property.prop_title.ilike(furnished_pattern),
                        Property.prop_type.ilike(furnished_pattern),
                    )
                )
            else:
                query = query.filter(
                    ~or_(
                        Property.prop_desc.ilike(furnished_pattern),
                        Property.prop_title.ilike(furnished_pattern),
                        Property.prop_type.ilike(furnished_pattern),
                    )
                )

    min_price = filters.get('min_price')
    max_price = filters.get('max_price')
    if min_price is not None or max_price is not None:
        price_expr = _build_property_price_expr()
        if min_price is not None:
            query = query.filter(price_expr >= float(min_price))
        if max_price is not None:
            query = query.filter(price_expr <= float(max_price))

    sort_value = filters.get('sort') or 'newest'
    if sort_value == 'oldest':
        query = query.order_by(Property.prop_id.asc())
    elif sort_value == 'price_low':
        query = query.order_by(_build_property_price_expr().asc(), Property.prop_id.desc())
    elif sort_value == 'price_high':
        query = query.order_by(_build_property_price_expr().desc(), Property.prop_id.desc())
    else:
        query = query.order_by(Property.prop_id.desc())

    return query


def _build_similar_properties(property_data, limit=3):
    current_property_id = property_data.get('prop_id')
    if not current_property_id:
        return []

    property_columns = get_table_columns('property')
    status_column = _property_status_column()
    if status_column and status_column in property_columns:
        if status_column == 'prop_status':
            status_filter = " AND LOWER(COALESCE(prop_status, 'available')) = 'available'"
        else:
            status_filter = f" AND LOWER(COALESCE({status_column}, '')) = 'active'"
    else:
        status_filter = ""

    current_city = _normalize_lookup(property_data.get('prop_location'))
    current_state = _normalize_lookup(property_data.get('prop_state'))
    current_category_id = property_data.get('category_id')
    current_category_name = _normalize_lookup(property_data.get('prop_type'))

    select_fields = ['prop_id', 'prop_title', 'prop_price', 'prop_location', 'prop_state', 'prop_type']
    if 'category_id' in property_columns:
        select_fields.append('category_id')

    room_cols = ['prop_bedroom', 'prop_bedrooms', 'bedroom', 'bedrooms', 'prop_bathroom', 'prop_bathrooms', 'bathroom', 'bathrooms', 'prop_toilet', 'toilets', 'prop_area', 'prop_area_unit']
    for col in room_cols:
        if col in property_columns and col not in select_fields:
            select_fields.append(col)

    fields_str = ', '.join(select_fields)

    query_text = f'''
        SELECT {fields_str}
        FROM property
        WHERE prop_id != :pid
        {status_filter}
        AND (
            (LOWER(TRIM(prop_location)) = :city AND :city != '')
            OR (LOWER(TRIM(prop_state)) = :state AND :state != '')
            OR (category_id = :cat_id AND :cat_id IS NOT NULL)
            OR (LOWER(TRIM(prop_type)) = :cat_name AND :cat_name != '')
        )
        ORDER BY
            CASE
                WHEN LOWER(TRIM(prop_location)) = :city AND :city != '' THEN 0
                WHEN LOWER(TRIM(prop_state)) = :state AND :state != '' THEN 1
                ELSE 2
            END ASC,
            prop_id DESC
        LIMIT :limit
    '''

    params = {
        'pid': current_property_id,
        'city': current_city,
        'state': current_state,
        'cat_id': current_category_id,
        'cat_name': current_category_name,
        'limit': limit,
    }

    try:
        candidate_rows = db.session.execute(text(query_text), params).mappings().all()
    except Exception:
        candidate_rows = []

    if not candidate_rows:
        return []

    pids = [row['prop_id'] for row in candidate_rows]
    cover_map = get_bulk_cover_images(pids)

    similar_properties = [
        _serialize_similar_property(dict(row), cover_image=cover_map.get(row['prop_id']))
        for row in candidate_rows
    ]
    return similar_properties


def login_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if not session.get('user_id'):
            _store_next_url()
            flash('Please log in or create an account to continue using KayHomes.', 'warning')
            return redirect(url_for('login'))
        return view_func(*args, **kwargs)
    wrapped_view._requires_login = True
    return wrapped_view


def _authenticated_entry_redirect():
    # Authenticated users should not revisit auth forms.
    return redirect(url_for('properties'))


@app.after_request
def _apply_authenticated_no_cache_headers(response):
    endpoint = request.endpoint or ''
    view_func = app.view_functions.get(endpoint)
    if session.get('user_id') and view_func and getattr(view_func, '_requires_login', False):
        response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        response.headers['Pragma'] = 'no-cache'
        response.headers['Expires'] = '0'
    return response


@app.context_processor
def inject_unread_count():
    unread_count = 0
    notification_unread_count = 0
    latest_notifications = []
    if session.get('user_id'):
        unread_count = _get_unread_message_count(session['user_id'])
        notification_unread_count = _get_unread_notification_count(session['user_id'])
        latest_notifications = _get_latest_notifications(session['user_id'], limit=10)

    endpoint = request.endpoint or ''
    view_func = app.view_functions.get(endpoint)
    is_authenticated_page = bool(view_func and getattr(view_func, '_requires_login', False))

    return {
        'unread_count': unread_count,
        'notification_unread_count': notification_unread_count,
        'latest_notifications': latest_notifications,
        'csrf_token': _ensure_csrf_token(),
        '_properties_page_params': _properties_page_params,
        'current_theme': get_current_theme(),
        'is_authenticated_page': is_authenticated_page,
    }


@app.route('/')
def home():
    ensure_property_image_table()
    ensure_property_specs_schema()
    try:
        property_rows = (
            Property.query
            .options(joinedload(Property.category))
            .filter(func.lower(func.coalesce(Property.prop_status, 'available')) == 'available')
            .order_by(Property.prop_id.desc())
            .limit(6)
            .all()
        )
    except Exception:
        property_rows = []

    featured_properties = [_serialize_property_model(row) for row in property_rows]

    users_count = 0
    property_count = 0
    agents_count = 0

    try:
        users_count = User.query.count() or 0
    except Exception:
        users_count = 0

    try:
        property_count = Property.query.count() or 0
    except Exception:
        property_count = 0

    role_column = None
    for candidate in ('role', 'user_role', 'user_type'):
        if hasattr(User, candidate):
            role_column = getattr(User, candidate)
            break

    try:
        if role_column is not None:
            agents_count = (
                User.query
                .filter(func.lower(role_column).in_(['agent', 'developer', 'vendor']))
                .count()
                or 0
            )
        else:
            agents_count = users_count
    except Exception:
        agents_count = 0

    return render_template(
        'index.html',
        title='Home',
        featured_properties=featured_properties,
        agents_count=agents_count,
        property_count=property_count,
        users_count=users_count,
    )


@app.route('/about/')
def about():
    return render_template('about.html', title='About')


@app.route('/contact/', methods=['GET', 'POST'])
@login_required
def contact():
    ensure_contact_message_table()

    current_user = get_current_user()
    default_name = ''
    default_email = ''
    if current_user:
        default_name = f"{current_user.user_fname} {current_user.user_lname}".strip()
        default_email = current_user.user_email or ''

    form_data = {
        'name': (request.form.get('name') or default_name).strip(),
        'email': (request.form.get('email') or default_email).strip(),
        'phone': (request.form.get('phone') or '').strip(),
        'subject': (request.form.get('subject') or '').strip(),
        'message': (request.form.get('message') or '').strip(),
    }

    if request.method == 'POST':
        name = form_data['name']
        email = form_data['email']
        phone = form_data['phone']
        subject = form_data['subject']
        message = form_data['message']

        errors = []

        if not name:
            errors.append('Please enter your name.')
        elif len(name) < 2 or len(name) > 100:
            errors.append('Name must be between 2 and 100 characters.')

        if not email:
            errors.append('Please enter your email address.')
        else:
            try:
                validate_email(email, check_deliverability=False)
            except EmailNotValidError:
                errors.append('Please enter a valid email address.')

        if phone:
            compact_phone = phone.replace(' ', '').replace('-', '').replace('(', '').replace(')', '')
            if len(compact_phone) < 7 or len(compact_phone) > 20 or not compact_phone.replace('+', '').isdigit():
                errors.append('Please enter a valid phone number or leave it blank.')

        if subject and len(subject) > 150:
            errors.append('Subject must be 150 characters or fewer.')

        if not message:
            errors.append('Please enter your message.')
        elif len(message) < 10:
            errors.append('Message must be at least 10 characters long.')

        if errors:
            for error in errors:
                flash(error, 'warning')
            return render_template(
                'contact.html',
                title='Contact',
                contact_form=form_data,
                contact_form_locked=bool(current_user),
            )

        try:
            contact_message = ContactMessage(
                name=name,
                email=email,
                phone=phone or None,
                subject=subject or None,
                message=message,
                status='Unread',
            )
            db.session.add(contact_message)
            db.session.commit()
            flash("Your message has been sent successfully. We'll get back to you shortly.", 'success')
            return redirect(url_for('contact'))
        except Exception:
            db.session.rollback()
            flash('Unable to send your message right now. Please try again later.', 'danger')

    return render_template(
        'contact.html',
        title='Contact',
        contact_form=form_data,
        contact_form_locked=bool(current_user),
    )


@app.route('/properties')
@app.route('/properties/')
def properties():
    ensure_property_image_table()
    ensure_category_schema_compatibility()
    page = _coerce_page(request.args.get('page', 1), default=1)
    selected_category_id = request.args.get('category_id', type=int)
    selected_category_name = (request.args.get('category') or '').strip()
    filters = _extract_property_filters(request.args)
    search_query = filters['q']

    try:
        category_rows = (
            db.session.query(
                Category,
                func.count(Property.prop_id).label('property_count')
            )
            .outerjoin(Property, Property.category_id == Category.cat_id)
            .group_by(Category.cat_id, Category.cat_name)
            .order_by(Category.cat_name.asc())
            .all()
        )
    except Exception:
        category_rows = []

    dynamic_categories = [row[0] for row in category_rows]
    category_counts = {row[0].cat_id: int(row[1] or 0) for row in category_rows}

    category_by_id = {cat.cat_id: cat for cat in dynamic_categories}
    category_by_name = {cat.cat_name.strip().lower(): cat for cat in dynamic_categories if cat.cat_name}

    selected_category_obj = None
    if selected_category_id:
        selected_category_obj = category_by_id.get(selected_category_id)
        if not selected_category_obj:
            selected_category_id = None
    elif selected_category_name:
        selected_category_obj = category_by_name.get(selected_category_name.lower())
        if selected_category_obj:
            selected_category_id = selected_category_obj.cat_id

    selected_category = 'All Rentals'
    if selected_category_obj:
        selected_category = selected_category_obj.cat_name

    states, state_lga_map = _get_state_lga_form_data()

    try:
        base_query = Property.query.options(joinedload(Property.category))
        query = _apply_properties_filters(base_query, filters, selected_category_obj=selected_category_obj)
        pagination = query.paginate(page=page, per_page=12, error_out=False)
        property_rows = pagination.items
        total_matches = pagination.total
    except Exception:
        property_rows = []
        total_matches = 0
        pagination = _build_pagination(0, page, 12)

    try:
        all_rentals_count = Property.query.count()
    except Exception:
        all_rentals_count = len(property_rows) if not selected_category_obj and not search_query else 0

    cover_map = get_bulk_cover_images([row.prop_id for row in property_rows])
    properties = [_serialize_property_model(row, cover_image=cover_map.get(row.prop_id)) for row in property_rows]
    result_count = int(total_matches or 0)

    if not properties:
        if selected_category_obj and not search_query:
            empty_message = 'No properties listed in this category.'
            empty_subtext = 'Try another category or be the first to post one.'
        elif selected_category_obj and search_query:
            empty_message = 'No properties match your search in this category.'
            empty_subtext = 'Try a different keyword or category.'
        elif search_query:
            empty_message = 'No properties match your search.'
            empty_subtext = 'Try a different keyword.'
        else:
            empty_message = 'No properties available right now.'
            empty_subtext = 'Be the first to post one.'
    else:
        empty_message = None
        empty_subtext = None

    post_login_history_fix = bool(session.pop('post_login_history_fix', False))

    return render_template(
        'properties.html',
        title='Properties',
        properties=properties,
        result_count=result_count,
        categories=dynamic_categories,
        category_counts=category_counts,
        all_rentals_count=all_rentals_count,
        selected_category=selected_category,
        selected_category_id=selected_category_id,
        search_query=search_query,
        filters=filters,
        states=states,
        state_lga_map=state_lga_map,
        empty_message=empty_message,
        empty_subtext=empty_subtext,
        post_login_history_fix=post_login_history_fix,
        pagination=pagination,
    )


@app.route('/saved-searches', methods=['GET'])
@login_required
def saved_searches():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    try:
        rows = (
            SavedSearch.query
            .filter_by(user_id=user.user_id)
            .order_by(SavedSearch.created_at.desc(), SavedSearch.search_id.desc())
            .all()
        )
    except Exception:
        rows = []

    saved_search_items = [_serialize_saved_search(row) for row in rows]

    return render_template(
        'saved_searches.html',
        title='Saved Searches',
        saved_searches=saved_search_items,
    )


@app.route('/saved-searches/save', methods=['POST'])
@login_required
def save_search():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    csrf_error = _validate_csrf_request('properties', _properties_page_params(_extract_property_filters(request.form)))
    if csrf_error:
        return csrf_error

    search_name = (request.form.get('name') or '').strip()
    if not search_name:
        flash('Please enter a name for this search.', 'warning')
        return redirect(url_for('properties', **_properties_query_params(_extract_property_filters(request.form))))

    if len(search_name) > 150:
        flash('Search name must be 150 characters or fewer.', 'warning')
        return redirect(url_for('properties', **_properties_query_params(_extract_property_filters(request.form))))

    filters = _extract_property_filters(request.form)

    normalized_name = search_name.lower()
    existing_search = (
        SavedSearch.query
        .filter_by(user_id=user.user_id)
        .filter(func.lower(func.trim(SavedSearch.name)) == normalized_name)
        .first()
    )
    if existing_search:
        flash('You already saved a search with that name.', 'warning')
        return redirect(url_for('properties', **_properties_query_params(filters)))

    try:
        saved_search = SavedSearch(
            user_id=user.user_id,
            name=search_name,
            q=filters.get('q') or None,
            state=filters.get('state') or None,
            lga=filters.get('lga') or None,
            property_type=filters.get('property_type') or None,
            bedrooms=filters.get('bedrooms'),
            bathrooms=filters.get('bathrooms'),
            min_price=filters.get('min_price'),
            max_price=filters.get('max_price'),
            furnished=(filters.get('furnished_raw') or None),
            sort=filters.get('sort') or None,
        )
        db.session.add(saved_search)
        db.session.commit()
        _create_notification(
            user.user_id,
            'saved_search_match',
            'Saved Search Active',
            f"Saved search \"{search_name}\" is active. Match alerts will arrive here soon.",
            link=url_for('saved_searches')
        )
        flash('Search saved successfully.', 'success')
    except Exception:
        db.session.rollback()
        flash('Unable to save that search right now.', 'danger')

    return redirect(url_for('properties', **_properties_query_params(filters)))


@app.route('/saved-searches/<int:search_id>/run', methods=['GET'])
@login_required
def run_saved_search(search_id):
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    saved_search = SavedSearch.query.filter_by(search_id=search_id, user_id=user.user_id).first()
    if not saved_search:
        abort(404)

    return redirect(url_for('properties', **_saved_search_filter_params(saved_search)))


@app.route('/saved-searches/<int:search_id>/delete', methods=['POST'])
@login_required
def delete_saved_search(search_id):
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    csrf_error = _validate_csrf_request('saved_searches')
    if csrf_error:
        return csrf_error

    try:
        saved_search = SavedSearch.query.filter_by(search_id=search_id, user_id=user.user_id).first()
        if not saved_search:
            abort(404)

        db.session.delete(saved_search)
        db.session.commit()
        flash('Saved search deleted.', 'success')
    except Exception:
        db.session.rollback()
        flash('Unable to delete that saved search right now.', 'danger')

    return redirect(url_for('saved_searches'))


@app.route('/analytics', methods=['GET'])
@login_required
def analytics():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    analytics_payload = _get_analytics_payload(user.user_id)
    return render_template(
        'analytics.html',
        title='Analytics Dashboard',
        metrics=analytics_payload['metrics'],
        chart_data=analytics_payload['chart_data'],
        top_property=analytics_payload['top_property'],
    )


@app.route('/notifications', methods=['GET'])
@login_required
def notifications():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    try:
        rows = (
            Notification.query
            .filter_by(user_id=user.user_id)
            .order_by(Notification.created_at.desc(), Notification.notification_id.desc())
            .all()
        )
    except Exception:
        rows = []

    notification_items = [_serialize_notification(row) for row in rows]
    return render_template('notifications.html', title='Notifications', notifications=notification_items)


@app.route('/notifications/<int:notification_id>/read', methods=['POST'])
@login_required
def mark_notification_read(notification_id):
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    csrf_error = _validate_csrf_request('notifications')
    if csrf_error:
        return csrf_error

    try:
        item = Notification.query.filter_by(notification_id=notification_id, user_id=user.user_id).first()
        if not item:
            abort(404)
        item.is_read = True
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash('Unable to update notification right now.', 'danger')

    return redirect(url_for('notifications'))


@app.route('/notifications/read-all', methods=['POST'])
@login_required
def mark_all_notifications_read():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    csrf_error = _validate_csrf_request('notifications')
    if csrf_error:
        return csrf_error

    try:
        (
            Notification.query
            .filter_by(user_id=user.user_id, is_read=False)
            .update({'is_read': True}, synchronize_session=False)
        )
        db.session.commit()
        flash('All notifications marked as read.', 'success')
    except Exception:
        db.session.rollback()
        flash('Unable to update notifications right now.', 'danger')

    return redirect(url_for('notifications'))


@app.route('/property-details/')
def property_details():
    return redirect(url_for('properties'))


@app.route('/post-property', defaults={'property_id': None}, methods=['GET', 'POST'])
@app.route('/post-property/<int:property_id>', methods=['GET', 'POST'])
@login_required
def post_property(property_id=None):
    ensure_category_schema_compatibility()
    ensure_state_lga_seed_data()
    ensure_property_specs_schema()

    try:
        categories = Category.query.order_by(Category.cat_name.asc()).all()
    except Exception:
        categories = []

    states, state_lga_map = _get_state_lga_form_data()

    user_id = session.get('user_id')
    property_data = None

    existing_images = []
    if property_id:
        try:
            property_data = db.session.execute(
                text('SELECT * FROM property WHERE prop_id = :pid AND prop_userid = :uid'),
                {'pid': property_id, 'uid': user_id}
            ).mappings().first()
        except Exception:
            property_data = None

        if not property_data:
            flash('You can only edit your own listings.', 'danger')
            return redirect(url_for('my_listings'))

        existing_images = get_property_images(property_id)

    if request.method == 'POST':
        csrf_error = _validate_csrf_request('post_property', {'property_id': property_id} if property_id else None)
        if csrf_error:
            return csrf_error

        prop_title = request.form.get('prop_title')
        category_id = request.form.get('category_id', type=int)
        listing_type = request.form.get('listing_type')
        prop_status = _normalize_property_status(request.form.get('prop_status'))
        prop_desc = request.form.get('prop_desc')
        prop_price = request.form.get('prop_price', '')

        # Clean the price before saving
        prop_price = (
            prop_price.replace('₦', '')
                    .replace('N', '')
                    .replace(',', '')
                    .strip()
        )

        try:
            prop_price = float(prop_price)
        except ValueError:
            flash("Please enter a valid property price.", "danger")
            return redirect(
                url_for('post_property', property_id=property_id)
                if property_id else
                url_for('post_property')
            )
        prop_location = (request.form.get('prop_location') or '').strip()
        prop_state = (request.form.get('prop_state') or '').strip()
        prop_lga = (request.form.get('prop_lga') or '').strip()
        prop_address = (request.form.get('prop_address') or '').strip()

        def _safe_int(val):
            if val is None or str(val).strip() == '':
                return None
            try:
                return int(float(str(val).strip()))
            except (ValueError, TypeError):
                return None

        prop_bedroom = _safe_int(request.form.get('prop_bedroom') or request.form.get('bedrooms'))
        prop_bathroom = _safe_int(request.form.get('prop_bathroom') or request.form.get('bathrooms'))
        prop_toilet = _safe_int(request.form.get('prop_toilet') or request.form.get('toilets'))
        prop_area = _safe_int(request.form.get('prop_area') or request.form.get('area_sqm'))
        prop_area_unit = (request.form.get('prop_area_unit') or 'sqm').strip()

        if not (prop_title and category_id and listing_type and prop_desc and prop_price and prop_location and prop_state and prop_lga and prop_address):
            flash('Please fill all required fields', 'warning')
            return redirect(url_for('post_property', property_id=property_id) if property_id else url_for('post_property'))

        if prop_state and not prop_lga:
            flash('Please select a local government area for the selected state.', 'warning')
            return redirect(url_for('post_property', property_id=property_id) if property_id else url_for('post_property'))

        selected_category = Category.query.filter_by(cat_id=category_id).first()
        if not selected_category:
            flash('Please choose a valid category.', 'warning')
            return redirect(url_for('post_property', property_id=property_id) if property_id else url_for('post_property'))

        prop_type = selected_category.cat_name

        saved_property_id = None
        try:
            if property_data:
                property_obj = Property.query.filter_by(prop_id=property_id, prop_userid=user_id).first()
                if not property_obj:
                    flash('You can only edit your own listings.', 'danger')
                    return redirect(url_for('my_listings'))

                property_obj.prop_title = prop_title
                property_obj.category_id = category_id
                property_obj.prop_type = prop_type
                property_obj.listing_type = listing_type
                property_obj.prop_status = prop_status
                property_obj.prop_desc = prop_desc
                property_obj.prop_price = prop_price
                property_obj.prop_location = prop_location
                property_obj.prop_state = prop_state
                property_obj.prop_address = prop_address
                if hasattr(property_obj, 'prop_lga'):
                    property_obj.prop_lga = prop_lga
                if hasattr(property_obj, 'prop_bedroom'):
                    property_obj.prop_bedroom = prop_bedroom
                if hasattr(property_obj, 'bedrooms'):
                    property_obj.bedrooms = prop_bedroom
                if hasattr(property_obj, 'prop_bathroom'):
                    property_obj.prop_bathroom = prop_bathroom
                if hasattr(property_obj, 'bathrooms'):
                    property_obj.bathrooms = prop_bathroom
                if hasattr(property_obj, 'prop_toilet'):
                    property_obj.prop_toilet = prop_toilet
                if hasattr(property_obj, 'toilets'):
                    property_obj.toilets = prop_toilet
                if hasattr(property_obj, 'prop_area'):
                    property_obj.prop_area = prop_area
                if hasattr(property_obj, 'area_sqm'):
                    property_obj.area_sqm = prop_area
                if hasattr(property_obj, 'prop_area_unit'):
                    property_obj.prop_area_unit = prop_area_unit

                db.session.commit()
                saved_property_id = property_obj.prop_id

            else:
                property_payload = {
                    'prop_title': prop_title,
                    'category_id': category_id,
                    'prop_type': prop_type,
                    'listing_type': listing_type,
                    'prop_status': prop_status,
                    'prop_desc': prop_desc,
                    'prop_price': prop_price,
                    'prop_location': prop_location,
                    'prop_state': prop_state,
                    'prop_address': prop_address,
                    'prop_userid': user_id,
                }

                if hasattr(Property, 'prop_bedroom'):
                    property_payload['prop_bedroom'] = prop_bedroom
                if hasattr(Property, 'bedrooms'):
                    property_payload['bedrooms'] = prop_bedroom
                if hasattr(Property, 'prop_bathroom'):
                    property_payload['prop_bathroom'] = prop_bathroom
                if hasattr(Property, 'bathrooms'):
                    property_payload['bathrooms'] = prop_bathroom
                if hasattr(Property, 'prop_toilet'):
                    property_payload['prop_toilet'] = prop_toilet
                if hasattr(Property, 'toilets'):
                    property_payload['toilets'] = prop_toilet
                if hasattr(Property, 'prop_area'):
                    property_payload['prop_area'] = prop_area
                if hasattr(Property, 'area_sqm'):
                    property_payload['area_sqm'] = prop_area
                if hasattr(Property, 'prop_area_unit'):
                    property_payload['prop_area_unit'] = prop_area_unit
                if hasattr(Property, 'prop_lga'):
                    property_payload['prop_lga'] = prop_lga

                property_obj = Property(**property_payload)
                db.session.add(property_obj)
                db.session.commit()
                saved_property_id = property_obj.prop_id
        except Exception as e:
            app.logger.exception('Property save failed for user %s and property %s', user_id, property_id)

            db.session.rollback()
            try:
                ensure_property_specs_schema()
            except Exception:
                pass

            flash("Unable to save your property listing right now. Please try again.", "danger")
            return redirect(
                url_for('post_property', property_id=property_id)
                if property_id else
                url_for('post_property')
            )

        images = request.files.getlist('images')

        current_count = len(existing_images) if property_data else 0
        ok, error_message = save_property_images(saved_property_id, images, existing_count=current_count)
        if not ok:
            flash(error_message, 'danger')
            return redirect(url_for('post_property', property_id=saved_property_id))

        flash('Property updated successfully' if property_data else 'Property posted successfully', 'success')
        return redirect(url_for('my_listings' if property_data else 'properties'))

    return render_template(
        'post_property.html',
        title='Post Property' if not property_id else 'Edit Property',
        csrf_token=_ensure_csrf_token(),
        property_data=property_data,
        categories=categories,
        states=states,
        state_lga_map=state_lga_map,
        existing_images=existing_images,
        max_property_images=MAX_PROPERTY_IMAGES
    )


@app.route('/property/<int:property_id>')
def property_detail(property_id):
    t_start = time.perf_counter()

    t0 = time.perf_counter()
    ensure_property_image_table()
    ensure_property_specs_schema()
    ensure_property_reviews_table()
    current_user = session.get('user_id')
    try:
        stmt = text('''
            SELECT p.*, u.user_id AS owner_id, u.user_fname, u.user_lname, u.user_email, u.user_phone,
                   u.user_avatar, u.user_regdate, u.user_verified
            FROM property p
            JOIN users u ON p.prop_userid = u.user_id
            WHERE p.prop_id = :pid
        ''')
        result = db.session.execute(stmt, {'pid': property_id}).mappings().first()
    except Exception:
        result = None
    t_property_query = (time.perf_counter() - t0) * 1000.0

    if not result:
        abort(404)

    property_data = dict(result)

    raw_status = (property_data.get('prop_status') or 'available').strip().lower()
    is_owner = bool(current_user and current_user == property_data['owner_id'])
    is_admin = bool(session.get('admin_id'))

    if raw_status not in ('available', '') and not (is_owner or is_admin):
        flash('This property listing is currently pending approval or no longer available.', 'warning')
        return redirect(url_for('properties'))

    view_was_recorded = _record_property_view_once(property_id)
    property_data['prop_views'] = int(property_data.get('prop_views') or 0) + (1 if view_was_recorded else 0)
    property_data['status'] = _property_status_presentation(property_data.get('prop_status'))
    room_values = _property_room_values(property_data)
    property_data['bedrooms'] = room_values['bedrooms']
    property_data['bathrooms'] = room_values['bathrooms']
    property_data['toilets'] = room_values['toilets']
    property_data['area_sqm'] = room_values['area']
    owner = {
        'user_id': property_data['owner_id'],
        'user_fname': property_data['user_fname'],
        'user_lname': property_data['user_lname'],
        'user_email': property_data['user_email'],
        'user_phone': property_data['user_phone'],
        'user_avatar': property_data.get('user_avatar'),
        'avatar_url': _user_avatar_url(property_data.get('user_avatar')),
        'member_since_year': property_data['user_regdate'].year if hasattr(property_data.get('user_regdate'), 'year') else None,
        'is_verified': bool(property_data.get('user_verified')),
    }
    owner['quick_contact_links'] = _build_quick_contact_links(
        owner.get('user_phone'),
        property_data.get('prop_title') or 'this property',
        url_for('property_detail', property_id=property_id, _external=True),
    )

    is_favorite = False
    if current_user:
        try:
            favorite_check = db.session.execute(
                text('SELECT 1 FROM favorites WHERE fav_userid = :uid AND fav_propid = :pid LIMIT 1'),
                {'uid': current_user, 'pid': property_id}
            ).scalar()
            is_favorite = bool(favorite_check)
        except Exception:
            is_favorite = False

    image_rows = get_property_images(property_id)
    images = [row['image_path'] for row in image_rows]
    cover_image = images[0] if images else None
    cover_image_url = _upload_image_url(cover_image)
    gallery_images = [
        {
            'image_path': image_name,
            'image_url': _upload_image_url(image_name),
        }
        for image_name in images[1:]
    ] if len(images) > 1 else []

    similar_properties = _build_similar_properties(property_data, limit=3)
    can_message = bool(current_user and current_user != owner['user_id'])

    review_stats = _get_property_review_stats(property_id)
    all_reviews = _get_property_reviews(property_id)
    recent_reviews = all_reviews[:5]
    total_reviews_count = len(all_reviews)
    has_more_reviews = total_reviews_count > 5

    user_has_reviewed = False
    if current_user:
        try:
            rev_check = db.session.execute(
                text('SELECT 1 FROM property_reviews WHERE property_id = :pid AND reviewer_id = :uid LIMIT 1'),
                {'pid': property_id, 'uid': current_user}
            ).scalar()
            user_has_reviewed = bool(rev_check)
        except Exception:
            user_has_reviewed = False

    can_review = bool(current_user and current_user != owner['user_id'] and not user_has_reviewed)
    owner_review_stats = _get_owner_review_stats(owner['user_id'])
    owner_profile_stats = _get_profile_stats(owner['user_id'])
    owner['total_listings'] = owner_profile_stats.get('total_properties', 0)

    return render_template(
        'property-details.html',
        title=property_data.get('prop_title'),
        property=property_data,
        owner=owner,
        images=images,
        cover_image=cover_image,
        cover_image_url=cover_image_url,
        gallery_images=gallery_images,
        similar_properties=similar_properties,
        can_message=can_message,
        is_favorite=is_favorite,
        review_stats=review_stats,
        recent_reviews=recent_reviews,
        all_reviews=all_reviews,
        total_reviews_count=total_reviews_count,
        has_more_reviews=has_more_reviews,
        user_has_reviewed=user_has_reviewed,
        can_review=can_review,
        owner_review_stats=owner_review_stats,
        csrf_token=_ensure_csrf_token()
    )


@app.route('/property/<int:property_id>/review', methods=['POST'])
@login_required
def post_property_review(property_id):
    ensure_property_reviews_table()
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to leave a review.', 'warning')
        return redirect(url_for('login'))

    csrf_error = _validate_csrf_request('property_detail', {'property_id': property_id})
    if csrf_error:
        return csrf_error

    property_obj = Property.query.get(property_id)
    if not property_obj:
        flash('Property not found.', 'danger')
        return redirect(url_for('properties'))

    if property_obj.prop_userid == user_id:
        flash('Property owners cannot review their own property.', 'danger')
        return redirect(url_for('property_detail', property_id=property_id))

    existing_review = PropertyReview.query.filter_by(property_id=property_id, reviewer_id=user_id).first()
    if existing_review:
        flash('You have already submitted a review for this property.', 'warning')
        return redirect(url_for('property_detail', property_id=property_id))

    try:
        rating = int(request.form.get('rating', 0))
    except (ValueError, TypeError):
        rating = 0

    if rating < 1 or rating > 5:
        flash('Please select a valid star rating (1-5).', 'warning')
        return redirect(url_for('property_detail', property_id=property_id))

    review_text = (request.form.get('review_text') or '').strip()

    allowed_tags = {'Responsive', 'Accurate listing', 'Professional', 'On time', 'Good communication'}
    selected_tags = request.form.getlist('review_tags')
    valid_tags = [t for t in selected_tags if t in allowed_tags]
    tags_string = ', '.join(valid_tags) if valid_tags else None

    try:
        new_review = PropertyReview(
            property_id=property_id,
            reviewer_id=user_id,
            owner_id=property_obj.prop_userid,
            rating=rating,
            review_text=review_text if review_text else None,
            review_tags=tags_string,
            is_visible=True,
        )
        db.session.add(new_review)
        db.session.commit()
        _create_notification(
            property_obj.prop_userid,
            'review',
            'New Property Review',
            f"Your listing \"{property_obj.prop_title}\" received a new {rating}-star review.",
            link=url_for('property_detail', property_id=property_id)
        )
        flash('Thank you! Your review has been submitted successfully.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.exception('Failed to save review for property %s by user %s', property_id, user_id)
        flash('Failed to submit review. Please try again.', 'danger')

    return redirect(url_for('property_detail', property_id=property_id))


@app.route('/property-details/<int:property_id>')
def property_details_alias(property_id):
    return redirect(url_for('property_detail', property_id=property_id))


@app.route('/favorite/toggle/<int:property_id>', methods=['POST'])
@login_required
def favorite_toggle(property_id):
    user_id = session['user_id']
    csrf_error = _validate_csrf_request('property_detail', {'property_id': property_id}, expect_json=True)
    if csrf_error:
        return csrf_error
    try:
        property_row = db.session.execute(
            text('SELECT prop_userid, prop_title FROM property WHERE prop_id = :pid LIMIT 1'),
            {'pid': property_id}
        ).mappings().first()

        if not property_row:
            return jsonify({'error': 'Property not found'}), 404

        existing = db.session.execute(
            text('SELECT 1 FROM favorites WHERE fav_userid = :uid AND fav_propid = :pid LIMIT 1'),
            {'uid': user_id, 'pid': property_id}
        ).scalar()

        if existing:
            db.session.execute(
                text('DELETE FROM favorites WHERE fav_userid = :uid AND fav_propid = :pid'),
                {'uid': user_id, 'pid': property_id}
            )
            db.session.commit()
            return jsonify({'is_favorite': False, 'status': 'removed'})

        db.session.execute(
            text('INSERT INTO favorites (fav_userid, fav_propid) VALUES (:uid, :pid)'),
            {'uid': user_id, 'pid': property_id}
        )
        db.session.commit()
        if int(property_row.get('prop_userid') or 0) != int(user_id):
            _create_notification(
                property_row.get('prop_userid'),
                'favorite',
                'Property Saved',
                f"Someone added your listing \"{property_row.get('prop_title') or 'property'}\" to favorites.",
                link=url_for('property_detail', property_id=property_id)
            )
        return jsonify({'is_favorite': True, 'status': 'added'})
    except Exception:
        db.session.rollback()
        return jsonify({'error': 'Unable to toggle favorite'}), 500


@app.route('/start-chat/<int:property_id>')
@login_required
def start_chat(property_id):
    user_id = session['user_id']
    try:
        prop = db.session.execute(
            text('SELECT prop_userid FROM property WHERE prop_id = :pid'),
            {'pid': property_id}
        ).mappings().first()
    except Exception:
        prop = None

    if not prop:
        abort(404)

    owner_id = prop['prop_userid']
    if owner_id == user_id:
        flash('This is your property listing', 'info')
        return redirect(url_for('property_detail', property_id=property_id))

    # If any message exists between these users for this property, go to chat.
    conversation_exists = db.session.execute(
        text('''
            SELECT 1 FROM messages
            WHERE property_id = :pid
              AND ((sender_id = :uid AND receiver_id = :oid) OR (sender_id = :oid AND receiver_id = :uid))
            LIMIT 1
        '''),
        {'pid': property_id, 'uid': user_id, 'oid': owner_id}
    ).scalar()

    return redirect(url_for('chat', property_id=property_id, user_id=owner_id))


@app.route('/chat/<int:property_id>/<int:user_id>', methods=['GET', 'POST'])
@login_required
def chat(property_id, user_id):
    current_user = session['user_id']
    if current_user == user_id:
        flash('Cannot chat with yourself', 'warning')
        return redirect(url_for('property_detail', property_id=property_id))

    try:
        property_row = db.session.execute(
            text('SELECT * FROM property WHERE prop_id = :pid'),
            {'pid': property_id}
        ).mappings().first()
    except Exception:
        property_row = None

    if not property_row:
        abort(404)

    try:
        other_user = db.session.execute(
            text('SELECT * FROM users WHERE user_id = :uid'),
            {'uid': user_id}
        ).mappings().first()
    except Exception:
        other_user = None

    if not other_user:
        abort(404)

    if request.method == 'POST':
        message_text = request.form.get('message')
        csrf_error = _validate_csrf_request('chat', {'property_id': property_id, 'user_id': user_id})
        if csrf_error:
            return csrf_error
        if message_text:
            try:
                db.session.execute(
                    text('''
                        INSERT INTO messages (property_id, sender_id, receiver_id, message, is_read, created_at)
                        VALUES (:pid, :sender, :receiver, :message, 0, NOW())
                    '''),
                    {
                        'pid': property_id,
                        'sender': current_user,
                        'receiver': user_id,
                        'message': message_text
                    }
                )
                db.session.commit()
                _create_notification(
                    user_id,
                    'message',
                    'New Inquiry Message',
                    f"You have a new message about \"{property_row.get('prop_title') or 'your listing'}\".",
                    link=url_for('chat', property_id=property_id, user_id=current_user)
                )
            except Exception:
                db.session.rollback()
        return redirect(url_for('chat', property_id=property_id, user_id=user_id))

    try:
        db.session.execute(
            text('''
                UPDATE messages
                SET is_read = 1
                WHERE receiver_id = :uid
                  AND property_id = :pid
            '''),
            {'uid': current_user, 'pid': property_id}
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    try:
        messages_rows = db.session.execute(
            text('''
                SELECT m.*, u.user_fname, u.user_lname
                FROM messages m
                JOIN users u ON m.sender_id = u.user_id
                WHERE m.property_id = :pid
                  AND ((m.sender_id = :uid AND m.receiver_id = :oid) OR (m.sender_id = :oid AND m.receiver_id = :uid))
                ORDER BY m.created_at ASC
            '''),
            {'pid': property_id, 'uid': current_user, 'oid': user_id}
        ).mappings().all()
        messages = [dict(row) for row in messages_rows]
    except Exception:
        messages = []

    last_message_id = messages[-1]['msg_id'] if messages else 0

    return render_template(
        'chat.html',
        property=property_row,
        other_user=other_user,
        messages=messages,
        current_user=current_user,
        last_message_id=last_message_id,
    )


@app.route('/api/messages/unread-count')
@login_required
def unread_message_count_api():
    user_id = session['user_id']
    return jsonify({'unread_count': _get_unread_message_count(user_id)})


@app.route('/api/chat/<int:property_id>/<int:user_id>/messages')
@login_required
def chat_messages_api(property_id, user_id):
    current_user = session['user_id']
    after_id = request.args.get('after_id', 0, type=int) or 0

    if current_user == user_id:
        return jsonify({'error': 'Cannot chat with yourself'}), 400

    try:
        property_row = db.session.execute(
            text('SELECT prop_id FROM property WHERE prop_id = :pid'),
            {'pid': property_id}
        ).mappings().first()
        other_user = db.session.execute(
            text('SELECT user_id FROM users WHERE user_id = :uid'),
            {'uid': user_id}
        ).mappings().first()
    except Exception:
        property_row = None
        other_user = None

    if not property_row or not other_user:
        abort(404)

    try:
        rows = db.session.execute(
            text(
                '''
                SELECT m.msg_id, m.sender_id, m.receiver_id, m.message, m.created_at
                FROM messages m
                WHERE m.property_id = :pid
                  AND m.msg_id > :after_id
                  AND ((m.sender_id = :uid AND m.receiver_id = :oid) OR (m.sender_id = :oid AND m.receiver_id = :uid))
                ORDER BY m.msg_id ASC
                '''
            ),
            {'pid': property_id, 'after_id': after_id, 'uid': current_user, 'oid': user_id}
        ).mappings().all()
    except Exception:
        rows = []

    try:
        db.session.execute(
            text(
                '''
                UPDATE messages
                SET is_read = 1
                WHERE receiver_id = :uid
                  AND property_id = :pid
                  AND sender_id = :oid
                '''
            ),
            {'uid': current_user, 'pid': property_id, 'oid': user_id}
        )
        db.session.commit()
    except Exception:
        db.session.rollback()

    payload = [_serialize_message_row(row, current_user) for row in rows]
    last_message_id = payload[-1]['msg_id'] if payload else after_id
    return jsonify({'messages': payload, 'last_message_id': last_message_id})


@app.route('/api/chat/<int:property_id>/<int:user_id>/send', methods=['POST'])
@login_required
def chat_send_api(property_id, user_id):
    current_user = session['user_id']
    message_text = (request.form.get('message') or '').strip()

    csrf_error = _validate_csrf_request('chat', {'property_id': property_id, 'user_id': user_id}, expect_json=True)
    if csrf_error:
        return csrf_error

    if current_user == user_id:
        return jsonify({'error': 'Cannot chat with yourself'}), 400

    if not message_text:
        return jsonify({'error': 'Message is required.'}), 400

    try:
        property_row = db.session.execute(
            text('SELECT prop_id FROM property WHERE prop_id = :pid'),
            {'pid': property_id}
        ).mappings().first()
        other_user = db.session.execute(
            text('SELECT user_id FROM users WHERE user_id = :uid'),
            {'uid': user_id}
        ).mappings().first()
    except Exception:
        property_row = None
        other_user = None

    if not property_row or not other_user:
        abort(404)

    try:
        db.session.execute(
            text(
                '''
                INSERT INTO messages (property_id, sender_id, receiver_id, message, is_read, created_at)
                VALUES (:pid, :sender, :receiver, :message, 0, NOW())
                '''
            ),
            {'pid': property_id, 'sender': current_user, 'receiver': user_id, 'message': message_text}
        )
        db.session.commit()
        _create_notification(
            user_id,
            'message',
            'New Inquiry Message',
            f"You have a new message about property #{property_id}.",
            link=url_for('chat', property_id=property_id, user_id=current_user)
        )
        row = db.session.execute(
            text(
                '''
                SELECT msg_id, sender_id, receiver_id, message, created_at
                FROM messages
                WHERE property_id = :pid AND sender_id = :sender AND receiver_id = :receiver
                ORDER BY msg_id DESC
                LIMIT 1
                '''
            ),
            {'pid': property_id, 'sender': current_user, 'receiver': user_id}
        ).mappings().first()
    except Exception as exc:
        db.session.rollback()
        app.logger.exception('Chat send failed for property %s between %s and %s', property_id, current_user, user_id)
        return jsonify({'error': str(exc)}), 500

    return jsonify({'success': True, 'message': _serialize_message_row(row, current_user)})


@app.route('/api/properties/updates')
def properties_updates_api():
    ensure_property_image_table()
    ensure_category_schema_compatibility()

    selected_category_id = request.args.get('category_id', type=int)
    selected_category_obj = None
    if selected_category_id:
        selected_category_obj = Category.query.filter_by(cat_id=selected_category_id).first()

    filters = _extract_property_filters(request.args)
    before_id = request.args.get('before_id', 0, type=int) or 0

    try:
        query = Property.query.options(joinedload(Property.category)).filter(Property.prop_id > before_id)
        query = _apply_properties_filters(query, filters, selected_category_obj=selected_category_obj)
        rows = query.all()
    except Exception:
        rows = []

    properties = [_serialize_property_model(row) for row in rows]
    latest_property_id = before_id
    if properties:
        latest_property_id = max(item['prop_id'] for item in properties)

    return jsonify({'properties': properties, 'latest_property_id': latest_property_id})


@app.route('/my-listings/<int:property_id>/status', methods=['POST'])
@login_required
def update_listing_status(property_id):
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    csrf_error = _validate_csrf_request('my_listings')
    if csrf_error:
        return csrf_error

    status_value = _normalize_property_status(request.form.get('prop_status'))

    try:
        property_item = Property.query.filter_by(prop_id=property_id, prop_userid=user.user_id).first()
        if not property_item:
            flash('Listing not found or access denied.', 'warning')
            return redirect(url_for('my_listings'))

        property_item.prop_status = status_value
        db.session.commit()
        flash('Listing availability updated.', 'success')
    except Exception:
        db.session.rollback()
        flash('Unable to update listing status right now.', 'danger')

    return redirect(url_for('my_listings'))


@app.route('/api/my-listings/updates')
@login_required
def my_listings_updates_api():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Authentication required.'}), 401

    page = _coerce_page(request.args.get('page', 1), default=1)
    payload = _build_my_listings_payload(user.user_id, page=page, per_page=12)
    return jsonify({'listings': payload['listings']})


@app.route('/api/property/<int:property_id>/details')
def property_detail_api(property_id):
    payload = _build_property_detail_payload(property_id, session.get('user_id'))
    if not payload:
        abort(404)
    return jsonify(payload)


@app.route('/api/profile/stats')
@login_required
def profile_stats_api():
    user = get_current_user()
    if not user:
        return jsonify({'error': 'Authentication required.'}), 401
    return jsonify(_get_profile_stats(user.user_id))


@app.route('/messages')
@login_required
def messages():
    current_user = session['user_id']
    try:
        rows = db.session.execute(
            text('''
                SELECT m.*, p.prop_title,
                       CASE WHEN m.sender_id = :uid THEN m.receiver_id ELSE m.sender_id END AS other_id,
                       u.user_fname, u.user_lname
                FROM messages m
                JOIN property p ON m.property_id = p.prop_id
                JOIN users u ON u.user_id = CASE WHEN m.sender_id = :uid THEN m.receiver_id ELSE m.sender_id END
                WHERE m.sender_id = :uid OR m.receiver_id = :uid
                ORDER BY m.created_at DESC
            '''),
            {'uid': current_user}
        ).mappings().all()
    except Exception:
        rows = []

    conversations = []
    seen = set()
    for row in rows:
        key = (row['property_id'], row['other_id'])
        if key in seen:
            continue
        seen.add(key)
        conversations.append({
            'property_id': row['property_id'],
            'prop_title': row['prop_title'],
            'other_id': row['other_id'],
            'other_name': f"{row['user_fname']} {row['user_lname']}",
            'last_message': row['message'],
            'last_time': row['created_at']
        })

    return render_template('messages.html', title='Messages', conversations=conversations)


@app.route('/register/', methods=['GET', 'POST'])
def register():

    if session.get('user_id'):
        return _authenticated_entry_redirect()

    if request.method == 'POST':

        csrf_error = _validate_csrf_request('register')
        if csrf_error:
            return csrf_error

        fname = request.form.get('fname')
        lname = request.form.get('lname')
        email = request.form.get('email')
        phone = request.form.get('phone')
        password = request.form.get('password')
        confirm_password = request.form.get('confirm_password')

        if password != confirm_password:
            flash('Passwords do not match', 'danger')
            return redirect(url_for('register'))

        existing_user = User.query.filter_by(
            user_email=email
        ).first()

        if existing_user:
            flash('Email already exists', 'warning')
            return redirect(url_for('register'))

        password_hash = generate_password_hash(password)

        new_user = User(
            user_fname=fname,
            user_lname=lname,
            user_email=email,
            user_phone=phone,
            user_pwd=password_hash
        )

        db.session.add(new_user)
        db.session.commit()

        flash('Registration successful! Please log in to continue.', 'success')
        return redirect(url_for('login'))

    return render_template('register.html', title='Register')


@app.route('/forgot-password', methods=['GET', 'POST'])
def forgot_password():
    form = ForgotPasswordForm()
    generic_message = 'If an account with that email exists, a password reset link has been sent.'

    if form.validate_on_submit():
        email = (form.email.data or '').strip().lower()
        user = User.query.filter(func.lower(User.user_email) == email).first()

        if not user:
            flash(generic_message, 'info')
            return redirect(url_for('forgot_password'))

        token_value = secrets.token_urlsafe(48)
        expires_at = datetime.utcnow() + timedelta(hours=1)

        try:
            PasswordResetToken.query.filter(
                PasswordResetToken.user_id == user.user_id,
                PasswordResetToken.used.is_(False)
            ).update({'used': True}, synchronize_session=False)

            reset_token = PasswordResetToken(
                user_id=user.user_id,
                token=token_value,
                expires_at=expires_at,
                used=False,
            )
            db.session.add(reset_token)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            app.logger.exception('Forgot password token save failed for email %s: %s', email, e)
            flash('Unable to process your request right now. Please try again later.', 'danger')
            return redirect(url_for('forgot_password'))

        try:
            _send_password_reset_email(user, token_value)
            flash(generic_message, 'info')
        except Exception as e:
            app.logger.exception('Forgot password email send failed for email %s: %s', email, e)

            # Keep development unblocked even when SMTP is not configured.
            if app.config.get('MAIL_SUPPRESS_SEND'):
                reset_link = url_for('reset_password', token=token_value, _external=True)
                app.logger.info('Password reset link for %s: %s', email, reset_link)
                flash(generic_message, 'info')
            else:
                flash('Unable to send reset email right now. Please try again later.', 'danger')

        return redirect(url_for('forgot_password'))

    if request.method == 'POST' and form.errors:
        flash('Please enter a valid email address.', 'warning')

    return render_template('forgot_password.html', title='Forgot Password', form=form)


@app.route('/reset-password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    now_utc = datetime.utcnow()
    reset_token = PasswordResetToken.query.filter_by(token=token).first()

    invalid_token = (
        reset_token is None
        or reset_token.used
        or reset_token.expires_at is None
        or reset_token.expires_at < now_utc
        or reset_token.user is None
    )

    if invalid_token:
        return render_template('reset_password.html', title='Reset Password', form=None, invalid_token=True), 400

    form = ResetPasswordForm()
    if form.validate_on_submit():
        try:
            reset_token.user.user_pwd = generate_password_hash(form.password.data)
            reset_token.used = True
            db.session.commit()
            flash('Your password has been reset successfully. Please log in.', 'success')
            return redirect(url_for('login'))
        except Exception as e:
            db.session.rollback()
            app.logger.exception('Reset password failed for token %s: %s', token, e)
            flash('Unable to reset password right now. Please try again later.', 'danger')
            return redirect(url_for('forgot_password'))

    if request.method == 'POST' and form.errors:
        flash('Please correct the password fields and try again.', 'warning')

    return render_template('reset_password.html', title='Reset Password', form=form, invalid_token=False, token=token)



@app.route('/login/', methods=['GET', 'POST'])
def login():

    if session.get('user_id'):
        return _authenticated_entry_redirect()

    if request.method == 'POST':

        csrf_error = _validate_csrf_request('login')
        if csrf_error:
            return csrf_error

        email = request.form.get('email')
        password = request.form.get('password')

        user = User.query.filter_by(
            user_email=email
        ).first()

        if user and check_password_hash(
            user.user_pwd,
            password
        ):

            session['user_id'] = user.user_id
            session['user_name'] = user.user_fname
            session['theme'] = _normalized_theme(getattr(user, 'theme', 'light'))
            session['post_login_history_fix'] = True
            flash('Welcome back.', 'success')
            return _redirect_after_auth('properties')

        flash('Invalid email or password.', 'danger')

    return render_template('login.html', title='Login')


@app.route('/logout/')
def logout():
    session.clear()
    return redirect(url_for('home'))


@app.route('/profile/', methods=['GET', 'POST'])
@login_required
def profile():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    if request.method == 'POST':
        csrf_error = _validate_csrf_request('profile')
        if csrf_error:
            return csrf_error

        first_name = request.form.get('first_name', '').strip()
        last_name = request.form.get('last_name', '').strip()
        email = request.form.get('email', '').strip()
        phone = request.form.get('phone', '').strip()

        if not first_name or not last_name or not email:
            flash('Please complete the required profile fields.', 'warning')
            return redirect(url_for('profile'))

        existing_email = User.query.filter(User.user_email == email, User.user_id != user.user_id).first()
        if existing_email:
            flash('That email address is already in use.', 'warning')
            return redirect(url_for('profile'))

        user.user_fname = first_name
        user.user_lname = last_name
        user.user_email = email
        user.user_phone = phone
        db.session.commit()

        session['user_name'] = user.user_fname
        flash('Profile updated successfully.', 'success')
        return redirect(url_for('profile'))

    stats = _get_profile_stats(user.user_id)
    owner_review_stats = _get_owner_review_stats(user.user_id)

    return render_template(
        'profile.html',
        title='Profile',
        user=user,
        user_avatar_url=_user_avatar_url(getattr(user, 'user_avatar', None)),
        is_verified=bool(getattr(user, 'user_verified', False)),
        total_properties=stats['total_properties'],
        active_listings=stats['active_listings'],
        favorites_count=stats['favorites_count'],
        unread_messages=stats['unread_messages'],
        views_count=stats['views_count'],
        owner_review_stats=owner_review_stats,
    )


@app.route('/profile/avatar', methods=['POST'])
@login_required
def update_profile_avatar():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    csrf_error = _validate_csrf_request('profile')
    if csrf_error:
        return csrf_error

    uploaded_file = request.files.get('avatar')
    new_avatar_name, error_message = _save_user_avatar(uploaded_file)
    if error_message:
        flash(error_message, 'warning')
        return redirect(url_for('profile'))

    old_avatar = (getattr(user, 'user_avatar', None) or '').strip()

    try:
        user.user_avatar = new_avatar_name
        db.session.commit()
    except Exception:
        db.session.rollback()
        _delete_avatar_file(new_avatar_name)
        flash('Unable to update avatar right now. Please try again.', 'danger')
        return redirect(url_for('profile'))

    if old_avatar and old_avatar != new_avatar_name:
        _delete_avatar_file(old_avatar)

    flash('Profile photo updated successfully.', 'success')
    return redirect(url_for('profile'))


@app.route('/profile/avatar/remove', methods=['POST'])
@login_required
def remove_profile_avatar():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    csrf_error = _validate_csrf_request('profile')
    if csrf_error:
        return csrf_error

    current_avatar = (getattr(user, 'user_avatar', None) or '').strip()
    if not current_avatar:
        return redirect(url_for('profile'))

    try:
        user.user_avatar = None
        db.session.commit()
    except Exception:
        db.session.rollback()
        flash('Unable to remove avatar right now. Please try again.', 'danger')
        return redirect(url_for('profile'))

    _delete_avatar_file(current_avatar)
    flash('Profile photo removed successfully.', 'success')
    return redirect(url_for('profile'))


@app.route('/my-listings/')
@login_required
def my_listings():
    ensure_property_image_table()
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    page = _coerce_page(request.args.get('page', 1), default=1)
    payload = _build_my_listings_payload(user.user_id, page=page, per_page=12)

    return render_template(
        'my_listings.html',
        title='My Listings',
        listings=payload['listings'],
        pagination=payload['pagination'],
        current_user_avatar_url=_user_avatar_url(getattr(user, 'user_avatar', None)),
    )


@app.route('/my-favorites/')
@app.route('/favorites/')
@login_required
def my_favorites():
    ensure_property_image_table()
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    page = _coerce_page(request.args.get('page', 1), default=1)
    payload = _build_favorites_payload(user.user_id, page=page, per_page=12)

    return render_template('my_favorites.html', title='My Favorites', favorites=payload['favorites'], pagination=payload['pagination'])


@app.route('/account-settings/', methods=['GET', 'POST'])
@login_required
def account_settings():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    if request.method == 'POST':
        csrf_error = _validate_csrf_request('account_settings')
        if csrf_error:
            return csrf_error

        action = request.form.get('action', '').strip()

        if action == 'change_password':
            current_password = request.form.get('current_password', '')
            new_password = request.form.get('new_password', '')
            confirm_password = request.form.get('confirm_password', '')

            if not current_password or not new_password or not confirm_password:
                flash('Please complete all password fields.', 'warning')
                return redirect(url_for('account_settings'))

            if not check_password_hash(user.user_pwd, current_password):
                flash('Current password is incorrect.', 'danger')
                return redirect(url_for('account_settings'))

            if len(new_password) < 8:
                flash('New password must be at least 8 characters.', 'warning')
                return redirect(url_for('account_settings'))

            if new_password != confirm_password:
                flash('New password and confirmation do not match.', 'warning')
                return redirect(url_for('account_settings'))

            user.user_pwd = generate_password_hash(new_password)
            db.session.commit()
            flash('Password changed successfully.', 'success')
            return redirect(url_for('account_settings'))

        if action == 'change_email':
            new_email = request.form.get('new_email', '').strip()
            account_password = request.form.get('account_password', '')

            if not new_email or not account_password:
                flash('Please provide your new email and current password.', 'warning')
                return redirect(url_for('account_settings'))

            if not check_password_hash(user.user_pwd, account_password):
                flash('Password verification failed.', 'danger')
                return redirect(url_for('account_settings'))

            existing_email = User.query.filter(User.user_email == new_email, User.user_id != user.user_id).first()
            if existing_email:
                flash('That email address is already in use.', 'warning')
                return redirect(url_for('account_settings'))

            if user.user_email == new_email:
                flash('This is already your current email address.', 'info')
                return redirect(url_for('account_settings'))

            user.user_email = new_email
            db.session.commit()
            flash('Email address updated successfully.', 'success')
            return redirect(url_for('account_settings'))

        if action == 'delete_account':
            confirm_text = request.form.get('confirm_text', '').strip()
            account_password = request.form.get('delete_password', '')

            if confirm_text != 'DELETE':
                flash('Type DELETE to confirm account deletion.', 'warning')
                return redirect(url_for('account_settings'))

            if not check_password_hash(user.user_pwd, account_password):
                flash('Password verification failed. Account not deleted.', 'danger')
                return redirect(url_for('account_settings'))

            try:
                property_rows = db.session.execute(
                    text('SELECT prop_id FROM property WHERE prop_userid = :uid'),
                    {'uid': user.user_id}
                ).mappings().all()
                property_ids = [row['prop_id'] for row in property_rows]

                db.session.execute(text('DELETE FROM favorites WHERE fav_userid = :uid'), {'uid': user.user_id})

                if property_ids:
                    db.session.execute(
                        text('DELETE FROM favorites WHERE fav_propid IN :prop_ids').bindparams(prop_ids=tuple(property_ids), expanding=True)
                    )
                    db.session.execute(
                        text('DELETE FROM inquiries WHERE inqu_propid IN :prop_ids').bindparams(prop_ids=tuple(property_ids), expanding=True)
                    )

                    image_cols = get_property_image_columns()
                    if image_cols and image_cols['property_id']:
                        db.session.execute(
                            text(f"DELETE FROM property_image WHERE {image_cols['property_id']} IN :prop_ids").bindparams(prop_ids=tuple(property_ids), expanding=True)
                        )

                inquiry_columns = get_table_columns('inquiries')
                if 'inqu_userid' in inquiry_columns:
                    db.session.execute(text('DELETE FROM inquiries WHERE inqu_userid = :uid'), {'uid': user.user_id})

                db.session.execute(
                    text('DELETE FROM messages WHERE sender_id = :uid OR receiver_id = :uid'),
                    {'uid': user.user_id}
                )
                db.session.execute(text('DELETE FROM property WHERE prop_userid = :uid'), {'uid': user.user_id})
                db.session.delete(user)
                db.session.commit()

                session.clear()
                flash('Your account has been deleted.', 'success')
                return redirect(url_for('home'))
            except Exception:
                db.session.rollback()
                flash('Unable to delete your account right now. Please try again later.', 'danger')
                return redirect(url_for('account_settings'))

        flash('Invalid account settings action.', 'warning')
        return redirect(url_for('account_settings'))

    return render_template('account_settings.html', title='Account Settings', user=user)


@app.route('/settings/theme', methods=['POST'])
@login_required
def update_theme():
    user = get_current_user()
    if not user:
        return jsonify({'success': False, 'message': 'Authentication required.'}), 401

    csrf_error = _validate_csrf_request('account_settings', expect_json=True)
    if csrf_error:
        return csrf_error

    selected_theme = _normalized_theme(request.form.get('theme'))
    if selected_theme not in {'light', 'dark'}:
        return jsonify({'success': False, 'message': 'Invalid theme value.'}), 400

    try:
        user.theme = selected_theme
        db.session.commit()
        session['theme'] = selected_theme
        return jsonify({'success': True, 'theme': selected_theme})
    except Exception as e:
        db.session.rollback()
        app.logger.exception('Failed to update theme for user %s: %s', user.user_id, e)
        return jsonify({'success': False, 'message': 'Unable to save theme preference right now.'}), 500


@app.route('/listing/<int:property_id>/delete', methods=['POST'])
@login_required
def delete_listing(property_id):
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    csrf_error = _validate_csrf_request('my_listings')
    if csrf_error:
        return csrf_error

    try:
        property_row = db.session.execute(
            text('SELECT prop_userid FROM property WHERE prop_id = :pid'),
            {'pid': property_id}
        ).mappings().first()
    except Exception:
        property_row = None

    if not property_row or property_row['prop_userid'] != user.user_id:
        flash('You can only delete your own listings.', 'danger')
        return redirect(url_for('my_listings'))

    image_rows = get_property_images(property_id)
    image_paths = [row.get('image_path') for row in image_rows if row.get('image_path')]

    try:
        db.session.execute(text('DELETE FROM favorites WHERE fav_propid = :pid'), {'pid': property_id})
        db.session.execute(text('DELETE FROM inquiries WHERE inqu_propid = :pid'), {'pid': property_id})

        image_cols = get_property_image_columns()
        if image_cols and image_cols['property_id']:
            db.session.execute(text(f"DELETE FROM property_image WHERE {image_cols['property_id']} = :pid"), {'pid': property_id})

        db.session.execute(text('DELETE FROM property WHERE prop_id = :pid'), {'pid': property_id})
        db.session.commit()

        for image_path in image_paths:
            delete_image_file(image_path)

        flash('Listing deleted successfully.', 'success')
    except Exception:
        db.session.rollback()
        flash('Unable to delete listing right now.', 'danger')

    return redirect(url_for('my_listings'))


@app.route('/listing/<int:property_id>/edit')
@app.route('/edit-property/<int:property_id>')
@login_required
def edit_listing(property_id):
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    try:
        property_row = db.session.execute(
            text('SELECT * FROM property WHERE prop_id = :pid'),
            {'pid': property_id}
        ).mappings().first()
    except Exception:
        property_row = None

    if not property_row or property_row['prop_userid'] != user.user_id:
        flash('You can only edit your own listings.', 'danger')
        return redirect(url_for('my_listings'))

    return redirect(url_for('post_property', property_id=property_id))


@app.route('/listing/<int:property_id>/image/<int:image_id>/delete', methods=['POST'])
@login_required
def delete_listing_image(property_id, image_id):
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    csrf_error = _validate_csrf_request('post_property', {'property_id': property_id})
    if csrf_error:
        return csrf_error

    try:
        property_row = db.session.execute(
            text('SELECT prop_userid FROM property WHERE prop_id = :pid'),
            {'pid': property_id}
        ).mappings().first()
    except Exception:
        property_row = None

    if not property_row or property_row['prop_userid'] != user.user_id:
        flash('You can only edit your own listings.', 'danger')
        return redirect(url_for('my_listings'))

    cols = get_property_image_columns()
    if not cols or not cols['id'] or not cols['property_id'] or not cols['path']:
        flash('Image deletion is not supported by your current image table schema.', 'danger')
        return redirect(url_for('post_property', property_id=property_id))

    try:
        image_row = db.session.execute(
            text(f'''
                SELECT {cols['path']} AS image_path
                FROM property_image
                WHERE {cols['id']} = :iid AND {cols['property_id']} = :pid
                LIMIT 1
            '''),
            {'iid': image_id, 'pid': property_id}
        ).mappings().first()
    except Exception:
        image_row = None

    if not image_row:
        flash('Image not found.', 'warning')
        return redirect(url_for('post_property', property_id=property_id))

    try:
        db.session.execute(
            text(f'DELETE FROM property_image WHERE {cols["id"]} = :iid AND {cols["property_id"]} = :pid'),
            {'iid': image_id, 'pid': property_id}
        )
        db.session.commit()
        delete_image_file(image_row['image_path'])
        flash('Image deleted successfully.', 'success')
    except Exception:
        db.session.rollback()
        flash('Unable to delete image right now.', 'danger')

    return redirect(url_for('post_property', property_id=property_id))


@app.route('/favorite/remove/<int:property_id>', methods=['POST'])
@login_required
def remove_favorite(property_id):
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    csrf_error = _validate_csrf_request('my_favorites')
    if csrf_error:
        return csrf_error

    try:
        db.session.execute(
            text('DELETE FROM favorites WHERE fav_userid = :uid AND fav_propid = :pid'),
            {'uid': user.user_id, 'pid': property_id}
        )
        db.session.commit()
        flash('Property removed from favorites.', 'success')
    except Exception:
        db.session.rollback()
        flash('Unable to remove favorite right now.', 'danger')

    return redirect(url_for('my_favorites'))


@app.route('/user/')
@app.route('/dashboard/')
@login_required
def user_dashboard():
    return redirect(url_for('profile'))


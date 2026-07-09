from flask import render_template, request, redirect, url_for, session, flash, abort, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from pkg import app, ensure_category_schema_compatibility
from pkg.models import Category, ContactMessage, db, User, Property
import os, secrets
from werkzeug.utils import secure_filename
from sqlalchemy import text, inspect, or_, func
from sqlalchemy.orm import joinedload
from datetime import datetime
from functools import wraps
from email_validator import EmailNotValidError, validate_email


MAX_PROPERTY_IMAGES = 5
ALLOWED_IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp'}


def get_current_user():
    user_id = session.get('user_id')
    if not user_id:
        return None
    return User.query.get(user_id)


def _store_next_url():
    next_url = request.full_path if request.query_string else request.path
    if next_url.endswith('?'):
        next_url = next_url[:-1]
    session['next_url'] = next_url


def _redirect_after_auth(default_endpoint='home'):
    next_url = session.pop('next_url', None)
    if next_url:
        return redirect(next_url)
    return redirect(url_for(default_endpoint))


def get_table_columns(table_name):
    try:
        inspector = inspect(db.engine)
        if not inspector.has_table(table_name):
            return set()
        return {column['name'] for column in inspector.get_columns(table_name)}
    except Exception:
        return set()


def ensure_contact_message_table():
    try:
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
    except Exception:
        db.session.rollback()


def ensure_property_image_table():
    try:
        inspector = inspect(db.engine)
        if inspector.has_table('property_image'):
            cols = {column['name'] for column in inspector.get_columns('property_image')}
            fks = inspector.get_foreign_keys('property_image')

            property_ref_col = 'property_id' if 'property_id' in cols else ('pimg_propid' if 'pimg_propid' in cols else None)
            if not property_ref_col:
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
    except Exception:
        db.session.rollback()


def get_property_image_columns():
    cols = get_table_columns('property_image')
    if not cols:
        return None
    return {
        'id': 'image_id' if 'image_id' in cols else ('pimg_id' if 'pimg_id' in cols else None),
        'property_id': 'property_id' if 'property_id' in cols else ('pimg_propid' if 'pimg_propid' in cols else None),
        'path': 'image_path' if 'image_path' in cols else ('pimg_url' if 'pimg_url' in cols else None),
        'uploaded_at': 'uploaded_at' if 'uploaded_at' in cols else None,
    }


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
    upload_path = app.config['UPLOAD_FOLDER']
    try:
        for file_item in valid_files:
            safe_source_name = secure_filename(file_item.filename)
            unique_filename = generate_unique_upload_name(safe_source_name)
            file_path = os.path.join(upload_path, unique_filename)
            file_item.save(file_path)
            saved_files.append(unique_filename)

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
            db.session.execute(insert_stmt, {'pid': property_id, 'img': unique_filename})

        db.session.commit()
        return True, None
    except Exception as e:
        import traceback

        traceback.print_exc()
        print("IMAGE SAVE ERROR:", e)

        db.session.rollback()

        for filename in saved_files:
            try:
                os.remove(os.path.join(upload_path, filename))
            except Exception:
                pass

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


def login_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if not session.get('user_id'):
            _store_next_url()
            flash('Please log in or create an account to continue using KayHomes.', 'warning')
            return redirect(url_for('login'))
        return view_func(*args, **kwargs)
    return wrapped_view


@app.context_processor
def inject_unread_count():
    unread_count = 0
    if session.get('user_id'):
        try:
            unread_count = db.session.execute(
                text('SELECT COUNT(*) FROM messages WHERE receiver_id = :uid AND is_read = 0'),
                {'uid': session['user_id']}
            ).scalar() or 0
        except Exception:
            unread_count = 0
    return {'unread_count': unread_count}


@app.route('/')
def home():
    ensure_property_image_table()
    try:
        rows = db.session.execute(
            text('SELECT * FROM property ORDER BY prop_id DESC LIMIT 6')
        ).mappings().all()
    except Exception:
        rows = []

    featured_properties = []
    for row in rows:
        prop = dict(row)
        images = get_property_images(prop['prop_id'])
        prop['cover_image'] = images[0]['image_path'] if images else None
        featured_properties.append(prop)

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


@app.route('/properties/')
@login_required
def properties():
    ensure_property_image_table()
    ensure_category_schema_compatibility()
    selected_category_id = request.args.get('category_id', type=int)
    selected_category_name = (request.args.get('category') or '').strip()
    search_query = (request.args.get('q') or '').strip()

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

    try:
        query = Property.query.options(joinedload(Property.category))

        if selected_category_obj:
            query = query.filter(Property.category_id == selected_category_obj.cat_id)

        if search_query:
            like_pattern = f"%{search_query}%"
            query = query.outerjoin(Category, Property.category_id == Category.cat_id)
            query = query.filter(
                or_(
                    Property.prop_title.ilike(like_pattern),
                    Property.prop_location.ilike(like_pattern),
                    Property.prop_state.ilike(like_pattern),
                    Property.prop_desc.ilike(like_pattern),
                    Property.prop_address.ilike(like_pattern),
                    Property.prop_type.ilike(like_pattern),
                    Category.cat_name.ilike(like_pattern),
                )
            )

        property_rows = query.order_by(Property.prop_id.desc()).all()
    except Exception:
        property_rows = []

    try:
        all_rentals_count = Property.query.count()
    except Exception:
        all_rentals_count = len(property_rows) if not selected_category_obj and not search_query else 0

    properties = []
    for row in property_rows:
        prop = {
            'prop_id': row.prop_id,
            'prop_title': row.prop_title,
            'prop_type': row.category.cat_name if getattr(row, 'category', None) else row.prop_type,
            'listing_type': row.listing_type,
            'prop_desc': row.prop_desc,
            'prop_price': row.prop_price,
            'prop_location': row.prop_location,
            'prop_state': row.prop_state,
            'prop_address': row.prop_address,
        }
        images = get_property_images(row.prop_id)
        prop['cover_image'] = images[0]['image_path'] if images else None
        prop['short_desc'] = (row.prop_desc or '')[:160]
        properties.append(prop)

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

    return render_template(
        'properties.html',
        title='Properties',
        properties=properties,
        categories=dynamic_categories,
        category_counts=category_counts,
        all_rentals_count=all_rentals_count,
        selected_category=selected_category,
        selected_category_id=selected_category_id,
        search_query=search_query,
        empty_message=empty_message,
        empty_subtext=empty_subtext,
    )


@app.route('/property-details/')
@login_required
def property_details():
    return redirect(url_for('properties'))


@app.route('/post-property', defaults={'property_id': None}, methods=['GET', 'POST'])
@app.route('/post-property/<int:property_id>', methods=['GET', 'POST'])
@login_required
def post_property(property_id=None):
    ensure_category_schema_compatibility()

    try:
        categories = Category.query.order_by(Category.cat_name.asc()).all()
    except Exception:
        categories = []

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

        token = session.pop('csrf_token', None)
        form_token = request.form.get('csrf_token')
        if not token or not form_token or token != form_token:
            flash('Invalid CSRF token', 'danger')
            return redirect(url_for('post_property', property_id=property_id) if property_id else url_for('post_property'))

        prop_title = request.form.get('prop_title')
        category_id = request.form.get('category_id', type=int)
        listing_type = request.form.get('listing_type')
        prop_desc = request.form.get('prop_desc')
        prop_price = request.form.get('prop_price')
        prop_location = request.form.get('prop_location')
        prop_state = request.form.get('prop_state')
        prop_lga = request.form.get('prop_lga')
        prop_address = request.form.get('prop_address')

        if not (prop_title and category_id and listing_type and prop_desc and prop_price and prop_location and prop_state and prop_lga and prop_address):
            flash('Please fill all required fields', 'warning')
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
                property_obj.prop_desc = prop_desc
                property_obj.prop_price = prop_price
                property_obj.prop_location = prop_location
                property_obj.prop_state = prop_state
                property_obj.prop_address = prop_address
                db.session.commit()
                saved_property_id = property_obj.prop_id

            else:
                property_obj = Property(
                    prop_title=prop_title,
                    category_id=category_id,
                    prop_type=prop_type,
                    listing_type=listing_type,
                    prop_desc=prop_desc,
                    prop_price=prop_price,
                    prop_location=prop_location,
                    prop_state=prop_state,
                    prop_address=prop_address,
                    prop_userid=user_id,
                )
                db.session.add(property_obj)
                db.session.commit()
                saved_property_id = property_obj.prop_id
        except Exception:
            db.session.rollback()
            flash('Failed to save property', 'danger')
            return redirect(url_for('post_property', property_id=property_id) if property_id else url_for('post_property'))

        images = request.files.getlist('images')
        current_count = len(existing_images) if property_data else 0
        ok, error_message = save_property_images(saved_property_id, images, existing_count=current_count)
        if not ok:
            flash(error_message, 'danger')
            return redirect(url_for('post_property', property_id=saved_property_id))

        flash('Property updated successfully' if property_data else 'Property posted successfully', 'success')
        return redirect(url_for('my_listings' if property_data else 'properties'))

    import secrets as _secrets
    token = _secrets.token_hex(16)
    session['csrf_token'] = token
    return render_template(
        'post_property.html',
        title='Post Property' if not property_id else 'Edit Property',
        csrf_token=token,
        property_data=property_data,
        categories=categories,
        existing_images=existing_images,
        max_property_images=MAX_PROPERTY_IMAGES
    )


@app.route('/property/<int:property_id>')
@login_required
def property_detail(property_id):
    ensure_property_image_table()
    current_user = session.get('user_id')
    try:
        stmt = text('''
            SELECT p.*, u.user_id AS owner_id, u.user_fname, u.user_lname, u.user_email, u.user_phone
            FROM property p
            JOIN users u ON p.prop_userid = u.user_id
            WHERE p.prop_id = :pid
        ''')
        result = db.session.execute(stmt, {'pid': property_id}).mappings().first()
    except Exception:
        result = None

    if not result:
        abort(404)

    property_data = dict(result)
    owner = {
        'user_id': property_data['owner_id'],
        'user_fname': property_data['user_fname'],
        'user_lname': property_data['user_lname'],
        'user_email': property_data['user_email'],
        'user_phone': property_data['user_phone'],
    }

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
    gallery_images = images[1:] if len(images) > 1 else []

    try:
        other_rows = db.session.execute(
            text('SELECT * FROM property WHERE prop_userid = :uid AND prop_id != :pid LIMIT 6'),
            {'uid': owner['user_id'], 'pid': property_id}
        ).mappings().all()
        other_properties = []
        for row in other_rows:
            prop = dict(row)
            prop_images = get_property_images(prop['prop_id'])
            prop['cover_image'] = prop_images[0]['image_path'] if prop_images else None
            other_properties.append(prop)
    except Exception:
        other_properties = []

    can_message = current_user and current_user != owner['user_id']
    return render_template(
        'property-details.html',
        title=property_data.get('prop_title'),
        property=property_data,
        owner=owner,
        images=images,
        cover_image=cover_image,
        gallery_images=gallery_images,
        other_properties=other_properties,
        can_message=can_message,
        is_favorite=is_favorite
    )


@app.route('/property-details/<int:property_id>')
@login_required
def property_details_alias(property_id):
    return redirect(url_for('property_detail', property_id=property_id))


@app.route('/favorite/toggle/<int:property_id>', methods=['POST'])
@login_required
def favorite_toggle(property_id):
    user_id = session['user_id']
    try:
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

    return render_template(
        'chat.html',
        property=property_row,
        other_user=other_user,
        messages=messages,
        current_user=current_user
    )


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

    if request.method == 'POST':

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

        session['user_id'] = new_user.user_id
        session['user_name'] = new_user.user_fname
        flash('Registration successful.', 'success')
        return _redirect_after_auth('properties')

    return render_template('register.html', title='Register')



@app.route('/login/', methods=['GET', 'POST'])
def login():

    if request.method == 'POST':

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

    return render_template('profile.html', title='Profile', user=user)


@app.route('/my-listings/')
@login_required
def my_listings():
    ensure_property_image_table()
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    try:
        rows = db.session.execute(
            text('''
                SELECT p.*
                FROM property p
                WHERE p.prop_userid = :uid
                ORDER BY p.prop_id DESC
            '''),
            {'uid': user.user_id}
        ).mappings().all()
    except Exception:
        rows = []

    listings = []
    for row in rows:
        listings.append({
            'prop_id': row['prop_id'],
            'prop_title': row['prop_title'],
            'prop_price': row['prop_price'],
            'prop_location': row['prop_location'],
            'prop_type': row['prop_type'],
            'listing_type': row['listing_type'],
            'prop_userid': row['prop_userid'],
            'prop_desc': row['prop_desc'],
            'prop_state': row['prop_state'],
            'prop_address': row['prop_address'],
            'image': None,
            'created_at': row.get('prop_regdate')
        })

    for listing in listings:
        listing_images = get_property_images(listing['prop_id'])
        listing['image'] = listing_images[0]['image_path'] if listing_images else None

    try:
        inquiry_counts = db.session.execute(
            text('SELECT inqu_propid, COUNT(*) AS total FROM inquiries GROUP BY inqu_propid'),
            {}
        ).mappings().all()
    except Exception:
        inquiry_counts = []

    inquiry_map = {row['inqu_propid']: row['total'] for row in inquiry_counts if row.get('inqu_propid')}

    for listing in listings:
        listing['inquiry_count'] = inquiry_map.get(listing['prop_id'], 0)

    return render_template('my_listings.html', title='My Listings', listings=listings)


@app.route('/my-favorites/')
@app.route('/favorites/')
@login_required
def my_favorites():
    ensure_property_image_table()
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    try:
        rows = db.session.execute(
            text('''
                SELECT p.*, u.user_fname, u.user_lname
                FROM favorites f
                JOIN property p ON p.prop_id = f.fav_propid
                JOIN users u ON u.user_id = p.prop_userid
                WHERE f.fav_userid = :uid
                ORDER BY f.fav_id DESC
            '''),
            {'uid': user.user_id}
        ).mappings().all()
    except Exception:
        rows = []

    favorites = []
    for row in rows:
        favorites.append({
            'prop_id': row['prop_id'],
            'prop_title': row['prop_title'],
            'prop_price': row['prop_price'],
            'prop_location': row['prop_location'],
            'prop_type': row['prop_type'],
            'listing_type': row['listing_type'],
            'owner_name': f"{row['user_fname']} {row['user_lname']}",
            'image': None
        })

    for favorite in favorites:
        favorite_images = get_property_images(favorite['prop_id'])
        favorite['image'] = favorite_images[0]['image_path'] if favorite_images else None

    return render_template('my_favorites.html', title='My Favorites', favorites=favorites)


@app.route('/account-settings/', methods=['GET', 'POST'])
@login_required
def account_settings():
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

    if request.method == 'POST':
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


@app.route('/listing/<int:property_id>/delete', methods=['POST'])
@login_required
def delete_listing(property_id):
    user = get_current_user()
    if not user:
        return redirect(url_for('login'))

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


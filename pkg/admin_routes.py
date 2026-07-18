import os
import secrets
from functools import wraps

from flask import flash, jsonify, redirect, render_template, request, session, url_for
from sqlalchemy import func, inspect, or_, text
from email_validator import EmailNotValidError, validate_email
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

from pkg import app, ensure_admin_schema_compatibility, ensure_category_schema_compatibility
from pkg.models import Admin, Category, ContactMessage, Favorite, Property, User, db
from pkg.user_routes import (
    MAX_PROPERTY_IMAGES,
    delete_image_file,
    ensure_contact_message_table,
    ensure_property_image_table,
    get_property_image_columns,
    get_property_images,
    save_property_images,
)


def admin_required(view_func):
    @wraps(view_func)
    def wrapped(*args, **kwargs):
        admin_id = session.get('admin_id')
        if not admin_id:
            flash('Admin login required.', 'warning')
            return redirect(url_for('admin_login'))

        admin = db.session.get(Admin, admin_id)
        if not admin or (admin.status or 'Active') != 'Active':
            session.pop('admin_id', None)
            session.pop('admin_username', None)
            session.pop('admin_name', None)
            session.pop('admin_csrf_token', None)
            flash('Please sign in again.', 'warning')
            return redirect(url_for('admin_login'))

        return view_func(*args, **kwargs)

    return wrapped


def _property_columns():
    try:
        inspector = inspect(db.engine)
        if not inspector.has_table('property'):
            return set()
        return {column['name'] for column in inspector.get_columns('property')}
    except Exception:
        return set()


def _contact_message_query(search_query='', status_filter='All'):
    query = ContactMessage.query

    if status_filter in {'Unread', 'Read', 'Replied'}:
        query = query.filter(ContactMessage.status == status_filter)

    if search_query:
        like_pattern = f"%{search_query.lower()}%"
        query = query.filter(
            or_(
                ContactMessage.name.ilike(like_pattern),
                ContactMessage.email.ilike(like_pattern),
                ContactMessage.subject.ilike(like_pattern),
            )
        )

    return query.order_by(ContactMessage.created_at.desc(), ContactMessage.message_id.desc())


def _admin_display_name(admin):
    if not admin:
        return ''

    full_name = f"{(admin.first_name or '').strip()} {(admin.last_name or '').strip()}".strip()
    return full_name or (admin.username or 'Admin')


def _admin_csrf_token():
    token = session.get('admin_csrf_token')
    if not token:
        token = secrets.token_hex(32)
        session['admin_csrf_token'] = token
    return token


def _validate_admin_csrf():
    submitted_token = request.form.get('csrf_token') or ''
    session_token = session.get('admin_csrf_token') or ''
    if not submitted_token or submitted_token != session_token:
        flash('Your session expired. Please try again.', 'danger')
        return False
    return True


def _admin_account_counts():
    try:
        total_admins = Admin.query.count() or 0
    except Exception:
        total_admins = 0

    try:
        unread_messages = ContactMessage.query.filter(ContactMessage.status == 'Unread').count() or 0
    except Exception:
        unread_messages = 0

    return total_admins, unread_messages


def _admin_dashboard_stats_payload():
    total_properties = Property.query.count() or 0
    users_count = User.query.count() or 0

    try:
        favorites_count = Favorite.query.count() or 0
    except Exception:
        favorites_count = 0

    try:
        contact_messages_count = ContactMessage.query.count() or 0
    except Exception:
        contact_messages_count = 0

    try:
        unread_messages_count = ContactMessage.query.filter(ContactMessage.status == 'Unread').count() or 0
    except Exception:
        unread_messages_count = 0

    try:
        administrators_count = Admin.query.count() or 0
    except Exception:
        administrators_count = 0

    property_columns = _property_columns()
    status_column = None
    for candidate in ('status', 'prop_status', 'listing_status'):
        if candidate in property_columns:
            status_column = candidate
            break

    if status_column and hasattr(Property, status_column):
        active_listings = (
            Property.query
            .filter(getattr(Property, status_column) == 'Active')
            .count()
            or 0
        )
    else:
        active_listings = total_properties

    pending_approvals = None
    if status_column and hasattr(Property, status_column):
        try:
            pending_approvals = (
                Property.query
                .filter(func.lower(getattr(Property, status_column)).in_(['pending', 'awaiting approval', 'review']))
                .count()
                or 0
            )
        except Exception:
            pending_approvals = 0

    return {
        'total_properties': total_properties,
        'active_listings': active_listings,
        'users_count': users_count,
        'favorites_count': favorites_count,
        'contact_messages_count': contact_messages_count,
        'unread_messages_count': unread_messages_count,
        'administrators_count': administrators_count,
        'pending_approvals': pending_approvals,
    }


@app.context_processor
def inject_admin_context():
    admin_id = session.get('admin_id')
    current_admin = None
    if admin_id:
        try:
            current_admin = db.session.get(Admin, admin_id)
        except Exception:
            current_admin = None

    total_admins, unread_messages = _admin_account_counts()
    return {
        'admin_csrf_token': _admin_csrf_token(),
        'current_admin': current_admin,
        'admin_display_name': _admin_display_name(current_admin),
        'admin_total_count': total_admins,
        'admin_unread_messages_count': unread_messages,
    }


def _prepare_admin_form_data(form):
    return {
        'first_name': (form.get('first_name') or '').strip(),
        'last_name': (form.get('last_name') or '').strip(),
        'username': (form.get('username') or '').strip(),
        'email': (form.get('email') or '').strip(),
        'phone': (form.get('phone') or '').strip(),
        'password': form.get('password') or '',
        'confirm_password': form.get('confirm_password') or '',
        'status': (form.get('status') or 'Active').strip() or 'Active',
    }


def _validate_admin_account_data(data, admin_id=None, require_password=True):
    errors = []

    required_fields = ['first_name', 'last_name', 'username', 'email']
    if require_password:
        required_fields += ['password', 'confirm_password']

    for field_name in required_fields:
        if not (data.get(field_name) or '').strip():
            errors.append(f"{field_name.replace('_', ' ').title()} is required.")

    if data.get('email'):
        try:
            validate_email(data['email'], check_deliverability=False)
        except EmailNotValidError:
            errors.append('Please provide a valid email address.')

    if data.get('password') or require_password:
        if len(data.get('password') or '') < 8:
            errors.append('Password must be at least 8 characters.')
        if (data.get('password') or '') != (data.get('confirm_password') or ''):
            errors.append('Password and Confirm Password must match.')

    if data.get('username'):
        username_exists = Admin.query.filter(Admin.username == data['username'])
        if admin_id:
            username_exists = username_exists.filter(Admin.admin_id != admin_id)
        if username_exists.first():
            errors.append('Username already exists.')

    if data.get('email'):
        email_exists = Admin.query.filter(Admin.email == data['email'])
        if admin_id:
            email_exists = email_exists.filter(Admin.admin_id != admin_id)
        if email_exists.first():
            errors.append('Email already exists.')

    return errors


def _build_admin_payload(data, existing_admin=None):
    payload = {
        'first_name': data['first_name'],
        'last_name': data['last_name'],
        'username': data['username'],
        'email': data['email'],
        'phone': data['phone'] or None,
        'status': data.get('status') or 'Active',
    }

    if existing_admin is None or data.get('password'):
        payload['password'] = generate_password_hash(data['password'])

    return payload


def _category_name_exists(name, exclude_id=None):
    query = Category.query.filter(func.lower(Category.cat_name) == (name or '').lower())
    if exclude_id:
        query = query.filter(Category.cat_id != exclude_id)
    return query.first() is not None


@app.route('/admin/register', methods=['GET', 'POST'])
def admin_register():
    ensure_admin_schema_compatibility()

    admin_count = Admin.query.count() or 0
    if admin_count > 0:
        flash('Admin registration is disabled. Please log in.', 'warning')
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        if not _validate_admin_csrf():
            return redirect(url_for('admin_register'))

        form_data = _prepare_admin_form_data(request.form)
        errors = _validate_admin_account_data(form_data)
        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template('admin_register.html', title='Create Admin Account', form_data=form_data, mode='register')

        admin = Admin(**_build_admin_payload(form_data))
        admin.role = 'admin'
        admin.status = 'Active'

        try:
            db.session.add(admin)
            db.session.commit()
            flash('Admin account created successfully. Please log in.', 'success')
            return redirect(url_for('admin_login'))
        except Exception:
            db.session.rollback()
            flash('Unable to create admin account right now.', 'danger')

    return render_template('admin_register.html', title='Create Admin Account', form_data={}, mode='register')


@app.route('/admin/admins/create', methods=['GET', 'POST'])
@admin_required
def admin_create_admin():
    ensure_admin_schema_compatibility()

    if request.method == 'POST':
        if not _validate_admin_csrf():
            return redirect(url_for('admin_create_admin'))

        form_data = _prepare_admin_form_data(request.form)
        errors = _validate_admin_account_data(form_data)
        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template('admin_register.html', title='Create Admin Account', form_data=form_data, mode='create')

        admin = Admin(**_build_admin_payload(form_data))
        admin.role = 'admin'
        admin.status = 'Active'

        try:
            db.session.add(admin)
            db.session.commit()
            flash('Admin account created successfully.', 'success')
            return redirect(url_for('admin_management'))
        except Exception:
            db.session.rollback()
            flash('Unable to create admin account right now.', 'danger')

    return render_template('admin_register.html', title='Create Admin Account', form_data={}, mode='create')


@app.route('/admin/admins/')
@admin_required
def admin_management():
    ensure_admin_schema_compatibility()

    search_query = (request.args.get('q') or '').strip()
    status_filter = (request.args.get('status') or 'All').strip()

    query = Admin.query
    if search_query:
        like_pattern = f"%{search_query}%"
        query = query.filter(
            or_(
                Admin.first_name.ilike(like_pattern),
                Admin.last_name.ilike(like_pattern),
                Admin.username.ilike(like_pattern),
                Admin.email.ilike(like_pattern),
            )
        )

    if status_filter in {'Active', 'Inactive'}:
        query = query.filter(Admin.status == status_filter)

    admins = query.order_by(Admin.created_at.desc(), Admin.admin_id.desc()).all()

    return render_template(
        'admin_management.html',
        title='Admin Management',
        admins=admins,
        search_query=search_query,
        status_filter=status_filter,
        total_admins=Admin.query.count() or 0,
    )


@app.route('/admin/categories/')
@admin_required
def admin_categories():
    ensure_category_schema_compatibility()

    rows = (
        db.session.query(
            Category,
            func.count(Property.prop_id).label('property_count'),
        )
        .outerjoin(Property, Property.category_id == Category.cat_id)
        .group_by(Category.cat_id)
        .order_by(Category.cat_name.asc())
        .all()
    )

    categories = []
    for category, property_count in rows:
        categories.append({
            'cat_id': category.cat_id,
            'cat_name': category.cat_name,
            'cat_desc': category.cat_desc,
            'cat_date': category.cat_date,
            'property_count': int(property_count or 0),
        })

    return render_template('admin_categories.html', title='Manage Categories', categories=categories)


@app.route('/admin/categories/create', methods=['GET', 'POST'])
@admin_required
def admin_create_category():
    ensure_category_schema_compatibility()

    form_data = {'cat_name': '', 'cat_desc': ''}
    if request.method == 'POST':
        if not _validate_admin_csrf():
            return redirect(url_for('admin_create_category'))

        form_data['cat_name'] = (request.form.get('cat_name') or '').strip()
        form_data['cat_desc'] = (request.form.get('cat_desc') or '').strip()

        if not form_data['cat_name']:
            flash('Category name is required.', 'warning')
            return render_template('admin_category_form.html', title='Add Category', form_data=form_data, mode='create')

        if _category_name_exists(form_data['cat_name']):
            flash('Category name already exists.', 'warning')
            return render_template('admin_category_form.html', title='Add Category', form_data=form_data, mode='create')

        category = Category(cat_name=form_data['cat_name'], cat_desc=form_data['cat_desc'] or None)
        try:
            db.session.add(category)
            db.session.commit()
            flash('Category created successfully.', 'success')
            return redirect(url_for('admin_categories'))
        except Exception:
            db.session.rollback()
            flash('Unable to create category right now.', 'danger')

    return render_template('admin_category_form.html', title='Add Category', form_data=form_data, mode='create')


@app.route('/admin/categories/<int:category_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_category(category_id):
    ensure_category_schema_compatibility()

    category = Category.query.filter_by(cat_id=category_id).first()
    if not category:
        flash('Category not found.', 'danger')
        return redirect(url_for('admin_categories'))

    form_data = {'cat_name': category.cat_name or '', 'cat_desc': category.cat_desc or ''}
    if request.method == 'POST':
        if not _validate_admin_csrf():
            return redirect(url_for('admin_edit_category', category_id=category_id))

        form_data['cat_name'] = (request.form.get('cat_name') or '').strip()
        form_data['cat_desc'] = (request.form.get('cat_desc') or '').strip()

        if not form_data['cat_name']:
            flash('Category name is required.', 'warning')
            return render_template('admin_category_form.html', title='Edit Category', form_data=form_data, mode='edit', category=category)

        if _category_name_exists(form_data['cat_name'], exclude_id=category_id):
            flash('Category name already exists.', 'warning')
            return render_template('admin_category_form.html', title='Edit Category', form_data=form_data, mode='edit', category=category)

        try:
            category.cat_name = form_data['cat_name']
            category.cat_desc = form_data['cat_desc'] or None

            # Keep the legacy text column consistent for existing views and exports.
            db.session.execute(
                text('UPDATE property SET prop_type = :name WHERE category_id = :category_id'),
                {'name': form_data['cat_name'], 'category_id': category_id}
            )
            db.session.commit()
            flash('Category updated successfully.', 'success')
            return redirect(url_for('admin_categories'))
        except Exception:
            db.session.rollback()
            flash('Unable to update category right now.', 'danger')

    return render_template('admin_category_form.html', title='Edit Category', form_data=form_data, mode='edit', category=category)


@app.route('/admin/categories/<int:category_id>/delete', methods=['POST'])
@admin_required
def admin_delete_category(category_id):
    ensure_category_schema_compatibility()

    if not _validate_admin_csrf():
        return redirect(url_for('admin_categories'))

    category = Category.query.filter_by(cat_id=category_id).first()
    if not category:
        flash('Category not found.', 'danger')
        return redirect(url_for('admin_categories'))

    assigned_count = Property.query.filter(Property.category_id == category_id).count() or 0
    if assigned_count > 0:
        flash('Cannot delete this category because properties are assigned to it.', 'warning')
        return redirect(url_for('admin_categories'))

    try:
        db.session.delete(category)
        db.session.commit()
        flash('Category deleted successfully.', 'success')
    except Exception:
        db.session.rollback()
        flash('Unable to delete category right now.', 'danger')

    return redirect(url_for('admin_categories'))


@app.route('/admin/admins/<int:admin_id>/view')
@admin_required
def admin_view_admin(admin_id):
    admin = Admin.query.get(admin_id)
    if not admin:
        flash('Admin not found.', 'danger')
        return redirect(url_for('admin_management'))

    return render_template('admin_view_admin.html', title='View Admin', admin=admin)


@app.route('/admin/admins/<int:admin_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_admin(admin_id):
    admin = Admin.query.get(admin_id)
    if not admin:
        flash('Admin not found.', 'danger')
        return redirect(url_for('admin_management'))

    if request.method == 'POST':
        if not _validate_admin_csrf():
            return redirect(url_for('admin_edit_admin', admin_id=admin_id))

        form_data = _prepare_admin_form_data(request.form)
        form_data['password'] = ''
        form_data['confirm_password'] = ''
        errors = _validate_admin_account_data(form_data, admin_id=admin.admin_id, require_password=False)
        if errors:
            for error in errors:
                flash(error, 'danger')
            return render_template('admin_edit_admin.html', title='Edit Admin', admin=admin, form_data=form_data)

        admin.first_name = form_data['first_name']
        admin.last_name = form_data['last_name']
        admin.username = form_data['username']
        admin.email = form_data['email']
        admin.phone = form_data['phone'] or None
        admin.status = form_data['status'] or 'Active'

        uploaded_picture = request.files.get('profile_image')
        if uploaded_picture and uploaded_picture.filename:
            safe_name = secure_filename(uploaded_picture.filename)
            _, ext = os.path.splitext(safe_name)
            image_name = f"admin_{admin.admin_id}_{secrets.token_hex(8)}{ext.lower()}"
            relative_path = f"uploads/{image_name}"
            upload_path = os.path.join(app.config['UPLOAD_FOLDER'], image_name)
            uploaded_picture.save(upload_path)
            admin.profile_image = relative_path

        try:
            db.session.commit()
            flash('Admin profile updated successfully.', 'success')
            return redirect(url_for('admin_management'))
        except Exception:
            db.session.rollback()
            flash('Unable to update admin profile right now.', 'danger')

    form_data = {
        'first_name': admin.first_name or '',
        'last_name': admin.last_name or '',
        'username': admin.username or '',
        'email': admin.email or '',
        'phone': admin.phone or '',
        'status': admin.status or 'Active',
    }

    return render_template('admin_edit_admin.html', title='Edit Admin', admin=admin, form_data=form_data)


@app.route('/admin/admins/<int:admin_id>/reset-password', methods=['GET', 'POST'])
@admin_required
def admin_reset_admin_password(admin_id):
    admin = Admin.query.get(admin_id)
    if not admin:
        flash('Admin not found.', 'danger')
        return redirect(url_for('admin_management'))

    if request.method == 'POST':
        if not _validate_admin_csrf():
            return redirect(url_for('admin_reset_admin_password', admin_id=admin_id))

        new_password = (request.form.get('new_password') or '').strip()
        confirm_password = (request.form.get('confirm_password') or '').strip()

        if not new_password or not confirm_password:
            flash('Please complete all password fields.', 'warning')
            return redirect(url_for('admin_reset_admin_password', admin_id=admin_id))

        if len(new_password) < 8:
            flash('Password must be at least 8 characters.', 'warning')
            return redirect(url_for('admin_reset_admin_password', admin_id=admin_id))

        if new_password != confirm_password:
            flash('Password and Confirm Password must match.', 'warning')
            return redirect(url_for('admin_reset_admin_password', admin_id=admin_id))

        try:
            admin.password = generate_password_hash(new_password)
            db.session.commit()
            flash('Password reset successfully.', 'success')
            return redirect(url_for('admin_management'))
        except Exception:
            db.session.rollback()
            flash('Unable to reset password right now.', 'danger')

    return render_template('admin_reset_password.html', title='Reset Password', admin=admin)


@app.route('/admin/admins/<int:admin_id>/delete', methods=['POST'])
@admin_required
def admin_delete_admin(admin_id):
    if session.get('admin_id') == admin_id:
        flash('You cannot delete your own account while logged in.', 'warning')
        return redirect(url_for('admin_management'))

    admin = Admin.query.get(admin_id)
    if not admin:
        flash('Admin not found.', 'danger')
        return redirect(url_for('admin_management'))

    if not _validate_admin_csrf():
        return redirect(url_for('admin_management'))

    try:
        db.session.delete(admin)
        db.session.commit()
        flash('Admin account deleted successfully.', 'success')
    except Exception:
        db.session.rollback()
        flash('Unable to delete admin right now.', 'danger')

    return redirect(url_for('admin_management'))


@app.route('/admin/login/', methods=['GET', 'POST'])
def admin_login():
    if session.get('admin_id'):
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        if not _validate_admin_csrf():
            return redirect(url_for('admin_login'))

        username = (request.form.get('username') or '').strip()
        password = request.form.get('password') or ''

        if not username or not password:
            flash('Please enter your username and password.', 'warning')
            return redirect(url_for('admin_login'))

        admin = Admin.query.filter_by(username=username).first()
        if not admin:
            flash('Invalid username or password.', 'danger')
            return redirect(url_for('admin_login'))

        if (admin.status or 'Active') != 'Active':
            flash('This admin account is inactive.', 'danger')
            return redirect(url_for('admin_login'))

        stored_password = admin.password or ''

        try:
            valid_password = check_password_hash(stored_password, password)
        except Exception:
            valid_password = False

        if not valid_password:
            flash('Invalid username or password.', 'danger')
            return redirect(url_for('admin_login'))

        session['admin_id'] = admin.admin_id
        session['admin_username'] = admin.username
        session['admin_name'] = _admin_display_name(admin)
        flash('Welcome back, admin.', 'success')
        return redirect(url_for('admin_dashboard'))

    return render_template('admin-login.html', title='Admin Login')


@app.route('/admin/')
@admin_required
def admin_dashboard():
    ensure_property_image_table()
    ensure_contact_message_table()

    stats = _admin_dashboard_stats_payload()
    total_properties = stats['total_properties']
    users_count = stats['users_count']
    favorites_count = stats['favorites_count']
    contact_messages_count = stats['contact_messages_count']
    unread_messages_count = stats['unread_messages_count']
    administrators_count = stats['administrators_count']
    active_listings = stats['active_listings']
    pending_approvals = stats['pending_approvals']

    property_columns = _property_columns()
    status_column = None
    for candidate in ('status', 'prop_status', 'listing_status'):
        if candidate in property_columns:
            status_column = candidate
            break

    properties = Property.query.order_by(Property.prop_id.desc()).all()

    posted_date_map = {}
    date_column = None
    for candidate in ('prop_regdate', 'created_at', 'date_posted'):
        if candidate in property_columns:
            date_column = candidate
            break

    if date_column:
        try:
            rows = db.session.execute(
                text(f'SELECT prop_id, {date_column} AS posted_date FROM property')
            ).mappings().all()
            posted_date_map = {row['prop_id']: row.get('posted_date') for row in rows}
        except Exception:
            posted_date_map = {}

    property_rows = []
    for item in properties:
        raw_status = 'Active'
        if status_column and hasattr(item, status_column):
            raw_status = getattr(item, status_column) or 'Active'

        property_rows.append({
            'prop_id': item.prop_id,
            'prop_title': item.prop_title,
            'prop_state': item.prop_state,
            'category': item.listing_type or item.prop_type or 'Property',
            'prop_type': item.prop_type,
            'prop_price': item.prop_price,
            'status': raw_status,
            'date_posted': posted_date_map.get(item.prop_id),
        })

    return render_template(
        'admin.html',
        title='Admin Dashboard',
        total_properties=total_properties,
        active_listings=active_listings,
        users_count=users_count,
        favorites_count=favorites_count,
        contact_messages_count=contact_messages_count,
        unread_messages_count=unread_messages_count,
        administrators_count=administrators_count,
        pending_approvals=pending_approvals,
        properties=property_rows,
    )


@app.route('/api/admin/dashboard/stats')
@admin_required
def admin_dashboard_stats_api():
    ensure_contact_message_table()
    return jsonify(_admin_dashboard_stats_payload())


@app.route('/admin/contact-messages/')
@app.route('/administrator/messages')
@admin_required
def admin_contact_messages():
    ensure_contact_message_table()

    try:
        search_query = (request.args.get('q') or '').strip()
        status_filter = (request.args.get('status') or 'All').strip()
        page = request.args.get('page', 1, type=int)

        query = _contact_message_query(search_query, status_filter)
        pagination = query.paginate(page=page, per_page=10, error_out=False)
        messages = pagination.items
    except Exception:
        messages = []
        pagination = None
        search_query = ''
        status_filter = 'All'

    unread_count = ContactMessage.query.filter(ContactMessage.status == 'Unread').count() or 0

    return render_template(
        'admin_contact_messages.html',
        title='Contact Messages',
        messages=messages,
        pagination=pagination,
        total_messages=ContactMessage.query.count() or 0,
        unread_messages=unread_count,
        search_query=search_query,
        status_filter=status_filter,
    )


@app.route('/admin/contact-messages/<int:message_id>')
@app.route('/administrator/messages/<int:message_id>')
@admin_required
def admin_view_contact_message(message_id):
    ensure_contact_message_table()

    message = ContactMessage.query.filter_by(message_id=message_id).first()
    if not message:
        flash('Contact message not found.', 'danger')
        return redirect(url_for('admin_contact_messages'))

    if (message.status or 'Unread') == 'Unread':
        try:
            message.status = 'Read'
            db.session.commit()
        except Exception:
            db.session.rollback()

    return render_template(
        'admin_contact_message_detail.html',
        title='View Message',
        message=message,
    )


@app.route('/admin/contact-messages/<int:message_id>/mark-replied', methods=['POST'])
@app.route('/administrator/messages/<int:message_id>/mark-replied', methods=['POST'])
@admin_required
def admin_mark_contact_message_replied(message_id):
    ensure_contact_message_table()

    if not _validate_admin_csrf():
        return redirect(url_for('admin_view_contact_message', message_id=message_id))

    message = ContactMessage.query.filter_by(message_id=message_id).first()
    if not message:
        flash('Contact message not found.', 'danger')
        return redirect(url_for('admin_contact_messages'))

    try:
        message.status = 'Replied'
        db.session.commit()
        flash('Message marked as replied successfully.', 'success')
    except Exception:
        db.session.rollback()
        flash('Unable to update message status right now.', 'danger')

    return redirect(url_for('admin_view_contact_message', message_id=message_id))


@app.route('/admin/contact-messages/<int:message_id>/delete', methods=['POST'])
@app.route('/administrator/messages/<int:message_id>/delete', methods=['POST'])
@admin_required
def admin_delete_contact_message(message_id):
    ensure_contact_message_table()

    if not _validate_admin_csrf():
        return redirect(url_for('admin_contact_messages'))

    message = ContactMessage.query.filter_by(message_id=message_id).first()
    if not message:
        flash('Contact message not found.', 'danger')
        return redirect(url_for('admin_contact_messages'))

    try:
        db.session.delete(message)
        db.session.commit()
        flash('Message deleted successfully.', 'success')
    except Exception:
        db.session.rollback()
        flash('Unable to delete message right now.', 'danger')

    return redirect(url_for('admin_contact_messages'))


@app.route('/admin/property/<int:property_id>/edit', methods=['GET', 'POST'])
@admin_required
def admin_edit_property(property_id):
    ensure_property_image_table()

    property_item = Property.query.filter_by(prop_id=property_id).first()
    if not property_item:
        flash('Property not found.', 'danger')
        return redirect(url_for('admin_dashboard'))

    property_columns = _property_columns()
    status_column = None
    for candidate in ('status', 'prop_status', 'listing_status'):
        if candidate in property_columns and hasattr(Property, candidate):
            status_column = candidate
            break

    if request.method == 'POST':
        if not _validate_admin_csrf():
            return redirect(url_for('admin_edit_property', property_id=property_id))

        property_item.prop_title = (request.form.get('prop_title') or '').strip()
        property_item.prop_desc = (request.form.get('prop_desc') or '').strip()
        property_item.prop_type = (request.form.get('prop_type') or '').strip()
        property_item.listing_type = (request.form.get('listing_type') or '').strip()
        property_item.prop_state = (request.form.get('prop_state') or '').strip()
        property_item.prop_location = (request.form.get('prop_location') or '').strip()
        property_item.prop_address = (request.form.get('prop_address') or '').strip()
        property_item.prop_price = (request.form.get('prop_price') or '').strip()

        if status_column:
            setattr(property_item, status_column, (request.form.get('status') or '').strip() or 'Active')

        required_fields = [
            property_item.prop_title,
            property_item.prop_desc,
            property_item.prop_type,
            property_item.listing_type,
            property_item.prop_state,
            property_item.prop_location,
            property_item.prop_address,
            property_item.prop_price,
        ]
        if not all(required_fields):
            flash('Please complete all required property fields.', 'warning')
            return redirect(url_for('admin_edit_property', property_id=property_id))

        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            flash('Unable to update property right now.', 'danger')
            return redirect(url_for('admin_edit_property', property_id=property_id))

        existing_images = get_property_images(property_id)
        images = request.files.getlist('images')
        ok, message = save_property_images(property_id, images, existing_count=len(existing_images))
        if not ok:
            flash(message, 'danger')
            return redirect(url_for('admin_edit_property', property_id=property_id))

        flash('Property updated successfully.', 'success')
        return redirect(url_for('admin_dashboard'))

    existing_images = get_property_images(property_id)

    property_payload = {
        'prop_id': property_item.prop_id,
        'prop_title': property_item.prop_title,
        'prop_desc': property_item.prop_desc,
        'prop_type': property_item.prop_type,
        'listing_type': property_item.listing_type,
        'prop_state': property_item.prop_state,
        'prop_location': property_item.prop_location,
        'prop_address': property_item.prop_address,
        'prop_price': property_item.prop_price,
        'status': getattr(property_item, status_column) if status_column else 'Active',
    }

    return render_template(
        'admin_edit_property.html',
        title='Edit Property',
        property_data=property_payload,
        existing_images=existing_images,
        max_property_images=MAX_PROPERTY_IMAGES,
        has_status=status_column is not None,
    )


@app.route('/admin/property/<int:property_id>/delete', methods=['POST'])
@admin_required
def admin_delete_property(property_id):
    property_item = Property.query.filter_by(prop_id=property_id).first()
    if not property_item:
        flash('Property not found.', 'danger')
        return redirect(url_for('admin_dashboard'))

    if not _validate_admin_csrf():
        return redirect(url_for('admin_dashboard'))

    image_rows = get_property_images(property_id)
    image_paths = [row.get('image_path') for row in image_rows if row.get('image_path')]

    try:
        db.session.execute(text('DELETE FROM favorites WHERE fav_propid = :pid'), {'pid': property_id})
        db.session.execute(text('DELETE FROM inquiries WHERE inqu_propid = :pid'), {'pid': property_id})

        image_cols = get_property_image_columns()
        if image_cols and image_cols['property_id']:
            db.session.execute(text(f"DELETE FROM property_image WHERE {image_cols['property_id']} = :pid"), {'pid': property_id})

        db.session.delete(property_item)
        db.session.commit()

        for image_path in image_paths:
            delete_image_file(image_path)

        flash('Property deleted successfully.', 'success')
    except Exception:
        db.session.rollback()
        flash('Unable to delete property right now.', 'danger')

    return redirect(url_for('admin_dashboard'))


@app.route('/admin/profile/', methods=['GET', 'POST'])
@admin_required
def admin_profile():
    admin_id = session.get('admin_id')
    admin = db.session.get(Admin, admin_id)
    if not admin:
        flash('Admin not found.', 'danger')
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        if not _validate_admin_csrf():
            return redirect(url_for('admin_profile'))

        first_name = (request.form.get('first_name') or '').strip()
        last_name = (request.form.get('last_name') or '').strip()
        username = (request.form.get('username') or '').strip()
        email = (request.form.get('email') or '').strip()
        phone = (request.form.get('phone') or '').strip()

        errors = []
        if not first_name:
            errors.append('First name is required.')
        if not last_name:
            errors.append('Last name is required.')
        if not username:
            errors.append('Username is required.')
        if not email:
            errors.append('Email is required.')
        else:
            try:
                validate_email(email, check_deliverability=False)
            except EmailNotValidError:
                errors.append('Please provide a valid email address.')

        if Admin.query.filter(Admin.username == username, Admin.admin_id != admin.admin_id).first():
            errors.append('Username already exists.')
        if Admin.query.filter(Admin.email == email, Admin.admin_id != admin.admin_id).first():
            errors.append('Email already exists.')

        if errors:
            for error in errors:
                flash(error, 'warning')
            return redirect(url_for('admin_profile'))

        admin.first_name = first_name
        admin.last_name = last_name
        admin.username = username
        admin.email = email
        admin.phone = phone or None

        uploaded_picture = request.files.get('profile_image')
        if uploaded_picture and uploaded_picture.filename:
            safe_name = secure_filename(uploaded_picture.filename)
            _, ext = os.path.splitext(safe_name)
            image_name = f"admin_{admin.admin_id}_{secrets.token_hex(8)}{ext.lower()}"
            relative_path = f"uploads/{image_name}"
            upload_path = os.path.join(app.config['UPLOAD_FOLDER'], image_name)
            uploaded_picture.save(upload_path)
            admin.profile_image = relative_path

        try:
            db.session.commit()
            session['admin_username'] = admin.username
            session['admin_name'] = _admin_display_name(admin)
            flash('Admin profile updated successfully.', 'success')
        except Exception:
            db.session.rollback()
            flash('Unable to update profile right now.', 'danger')

        return redirect(url_for('admin_profile'))

    profile_data = {
        'first_name': admin.first_name or '',
        'last_name': admin.last_name or '',
        'username': admin.username or '',
        'email': admin.email or '',
        'phone': admin.phone or '',
        'profile_image': admin.profile_image or '',
    }

    return render_template(
        'admin_profile.html',
        title='Admin Profile',
        profile=profile_data,
    )


@app.route('/admin/change-password/', methods=['GET', 'POST'])
@admin_required
def admin_change_password():
    admin = db.session.get(Admin, session.get('admin_id'))
    if not admin:
        flash('Admin account not found.', 'danger')
        return redirect(url_for('admin_login'))

    if request.method == 'POST':
        if not _validate_admin_csrf():
            return redirect(url_for('admin_change_password'))

        current_password = request.form.get('current_password') or ''
        new_password = request.form.get('new_password') or ''
        confirm_password = request.form.get('confirm_password') or ''

        if not current_password or not new_password or not confirm_password:
            flash('Please complete all password fields.', 'warning')
            return redirect(url_for('admin_change_password'))

        stored_password = admin.password or ''
        valid_current = False
        try:
            valid_current = check_password_hash(stored_password, current_password)
        except Exception:
            valid_current = False

        if not valid_current:
            flash('Current password is incorrect.', 'danger')
            return redirect(url_for('admin_change_password'))

        if len(new_password) < 8:
            flash('New password must be at least 8 characters.', 'warning')
            return redirect(url_for('admin_change_password'))

        if new_password != confirm_password:
            flash('New password and confirmation do not match.', 'warning')
            return redirect(url_for('admin_change_password'))

        admin.password = generate_password_hash(new_password)
        db.session.commit()

        flash('Password updated successfully.', 'success')
        return redirect(url_for('admin_dashboard'))

    return render_template('admin_change_password.html', title='Change Admin Password')


@app.route('/admin/logout/', methods=['GET', 'POST'])
@app.route('/admin/logout', methods=['GET', 'POST'])
def admin_logout():
    if request.method == 'POST' and not _validate_admin_csrf():
        return redirect(url_for('admin_dashboard') if session.get('admin_id') else url_for('admin_login'))

    session.pop('admin_id', None)
    session.pop('admin_username', None)
    session.pop('admin_name', None)
    session.pop('admin_csrf_token', None)
    return redirect(url_for('admin_login'))



from flask import render_template, request, redirect, url_for, session, flash, abort, jsonify
from werkzeug.security import generate_password_hash, check_password_hash
from pkg import app
from pkg.models import db, User
import os, secrets
from werkzeug.utils import secure_filename
from sqlalchemy import text, inspect
from datetime import datetime
from functools import wraps


def login_required(view_func):
    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if not session.get('user_id'):
            flash('Please log in to continue', 'warning')
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
    return render_template('index.html', title='Home')


@app.route('/about/')
def about():
    return render_template('about.html', title='About')


@app.route('/contact/')
def contact():
    return render_template('contact.html', title='Contact')


@app.route('/properties/')
def properties():
    # Fetch properties from DB
    try:
        result = db.session.execute(text('SELECT * FROM property ORDER BY prop_id DESC'))
        properties = result.mappings().all()
    except Exception:
        properties = []

    return render_template('properties.html', title='Properties', properties=properties)


@app.route('/property-details/')
def property_details():
    return redirect(url_for('properties'))


@app.route('/post-property', methods=['GET', 'POST'])
def post_property():

    if not session.get('user_id'):
        flash('You must be logged in to post a property', 'warning')
        return redirect(url_for('login'))

    if request.method == 'POST':

        # CSRF token check
        token = session.pop('csrf_token', None)
        form_token = request.form.get('csrf_token')
        if not token or not form_token or token != form_token:
            flash('Invalid CSRF token', 'danger')
            return redirect(url_for('post_property'))

        # gather form inputs mapped to DB columns
        prop_title = request.form.get('prop_title')
        prop_type = request.form.get('prop_type')
        listing_type = request.form.get('listing_type')
        prop_desc = request.form.get('prop_desc')
        prop_price = request.form.get('prop_price')
        prop_location = request.form.get('prop_location')
        prop_state = request.form.get('prop_state')
        prop_lga = request.form.get('prop_lga')
        prop_address = request.form.get('prop_address')

        # validate required
        if not (prop_title and prop_type and listing_type and prop_desc and prop_price and prop_location and prop_state and prop_lga and prop_address):
            flash('Please fill all required fields', 'warning')
            return redirect(url_for('post_property'))

        user_id = session.get('user_id')

        # Insert property row
        try:
            stmt = text('''INSERT INTO property (prop_title, prop_type, listing_type, prop_desc, prop_price, prop_location, prop_state, prop_address, prop_userid)
                           VALUES (:title, :ptype, :ltype, :desc, :price, :location, :state, :address, :userid)''')
            db.session.execute(stmt, {'title': prop_title, 'ptype': prop_type, 'ltype': listing_type, 'desc': prop_desc, 'price': prop_price, 'location': prop_location, 'state': prop_state, 'address': prop_address, 'userid': user_id})
            db.session.commit()

            # get last insert id
            last = db.session.execute(text('SELECT LAST_INSERT_ID()')).fetchone()
            prop_id = int(last[0]) if last else None
        except Exception:
            db.session.rollback()
            flash('Failed to post property', 'danger')
            return redirect(url_for('post_property'))

        # handle multiple images and insert into property_image
        images = request.files.getlist('images')
        if images:
            upload_path = app.config['UPLOAD_FOLDER']
            for img in images:
                if not img or not getattr(img, 'filename', None):
                    continue
                try:
                    fname = secure_filename(img.filename)
                    unique = secrets.token_hex(8)
                    _, ext = os.path.splitext(fname)
                    filename = f"{unique}{ext}"
                    img.save(os.path.join(upload_path, filename))
                    # insert image record
                    if prop_id:
                        img_stmt = text('INSERT INTO property_image (pimg_url, pimg_propid) VALUES (:url, :pid)')
                        db.session.execute(img_stmt, {'url': filename, 'pid': prop_id})
                except Exception:
                    # continue saving other images; log or flash minimal
                    continue
            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

        flash('Property posted successfully', 'success')
        return redirect(url_for('properties'))

    # GET -> render form with CSRF token
    import secrets as _secrets
    token = _secrets.token_hex(16)
    session['csrf_token'] = token
    return render_template('post_property.html', title='Post Property', csrf_token=token)


@app.route('/property/<int:property_id>')
def property_detail(property_id):
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

    try:
        image_rows = db.session.execute(
            text('SELECT pimg_url FROM property_image WHERE pimg_propid = :pid'),
            {'pid': property_id}
        ).mappings().all()
        images = [row['pimg_url'] for row in image_rows]
    except Exception:
        images = []

    try:
        other_rows = db.session.execute(
            text('SELECT * FROM property WHERE prop_userid = :uid AND prop_id != :pid LIMIT 6'),
            {'uid': owner['user_id'], 'pid': property_id}
        ).mappings().all()
        other_properties = [dict(row) for row in other_rows]
    except Exception:
        other_properties = []

    can_message = current_user and current_user != owner['user_id']
    return render_template(
        'property-details.html',
        title=property_data.get('prop_title'),
        property=property_data,
        owner=owner,
        images=images,
        other_properties=other_properties,
        can_message=can_message,
        is_favorite=is_favorite
    )


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

        flash('Registration successful', 'success')
        return redirect(url_for('login'))

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

            return redirect(url_for('user_dashboard'))

        flash('Invalid email or password')

    return render_template('login.html', title='Login')


@app.route('/logout/')
def logout():
    session.clear()
    return redirect(url_for('home'))


@app.route('/user/')
def user_dashboard():

    if not session.get('user_id'):
        return redirect(url_for('login'))

    return render_template('user.html', title='User Dashboard')


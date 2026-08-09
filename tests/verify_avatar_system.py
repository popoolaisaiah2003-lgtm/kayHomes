import io
import json
import os
from datetime import datetime

from sqlalchemy import text
from werkzeug.security import generate_password_hash

from pkg import app, db, initialize_database
from pkg.models import Category, Property, User


def _png_payload(seed):
    return b'\x89PNG\r\n\x1a\n' + f'avatar-{seed}'.encode('utf-8')


def run_verification():
    app.config['TESTING'] = True
    app.config['MAIL_SUPPRESS_SEND'] = True

    with app.app_context():
        initialize_database()

        inspector = db.inspect(db.engine)
        user_columns = [c['name'] for c in inspector.get_columns('users')]

        category = Category.query.order_by(Category.cat_id.asc()).first()
        if not category:
            category = Category(cat_name='Avatar Verify Category', cat_desc='Avatar verify')
            db.session.add(category)
            db.session.commit()

        email = f"avatar.verify.{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}@example.com"
        user = User(
            user_fname='Avatar',
            user_lname='Verifier',
            user_email=email,
            user_phone='08000000033',
            user_pwd=generate_password_hash('AvatarVerify123!'),
            user_verified=False,
        )
        db.session.add(user)
        db.session.commit()

        prop = Property(
            prop_title='Avatar Verify Property',
            category_id=category.cat_id,
            prop_type=category.cat_name,
            listing_type='Rent',
            prop_desc='Avatar verification listing',
            prop_price='250000',
            prop_location='Ikeja',
            prop_state='Lagos',
            prop_lga='Eti-Osa',
            prop_address='Avatar Street',
            prop_userid=user.user_id,
        )
        db.session.add(prop)
        db.session.commit()

        old_avatar_name = None
        new_avatar_name = None
        old_avatar_exists_after_replace = None
        profile_has_default = None
        profile_has_uploaded = None
        property_has_owner_avatar = None
        listings_has_avatar = None

        avatar_dir = app.config.get('AVATAR_UPLOAD_FOLDER')

        try:
            client = app.test_client()
            login_resp = client.post('/login/', data={'email': email, 'password': 'AvatarVerify123!'}, follow_redirects=False)

            profile_resp_before = client.get('/profile/', follow_redirects=False)
            profile_html_before = profile_resp_before.get_data(as_text=True)
            profile_has_default = 'default-avatar.png' in profile_html_before

            upload_1 = client.post(
                '/profile/avatar',
                data={'avatar': (io.BytesIO(_png_payload(1)), 'avatar1.png')},
                content_type='multipart/form-data',
                follow_redirects=False,
            )

            db.session.refresh(user)
            old_avatar_name = user.user_avatar
            old_avatar_path = os.path.join(avatar_dir, old_avatar_name) if old_avatar_name else None
            old_avatar_exists = bool(old_avatar_path and os.path.exists(old_avatar_path))

            upload_2 = client.post(
                '/profile/avatar',
                data={'avatar': (io.BytesIO(_png_payload(2)), 'avatar2.png')},
                content_type='multipart/form-data',
                follow_redirects=False,
            )

            db.session.refresh(user)
            new_avatar_name = user.user_avatar
            new_avatar_path = os.path.join(avatar_dir, new_avatar_name) if new_avatar_name else None
            new_avatar_exists = bool(new_avatar_path and os.path.exists(new_avatar_path))
            old_avatar_exists_after_replace = bool(old_avatar_path and os.path.exists(old_avatar_path))

            profile_resp_after = client.get('/profile/', follow_redirects=False)
            profile_html_after = profile_resp_after.get_data(as_text=True)
            profile_has_uploaded = bool(new_avatar_name and new_avatar_name in profile_html_after)

            detail_resp = client.get(f'/property/{prop.prop_id}', follow_redirects=False)
            detail_html = detail_resp.get_data(as_text=True)
            property_has_owner_avatar = bool(new_avatar_name and new_avatar_name in detail_html)

            listings_resp = client.get('/my-listings/', follow_redirects=False)
            listings_html = listings_resp.get_data(as_text=True)
            listings_has_avatar = bool(new_avatar_name and new_avatar_name in listings_html)

            return {
                'users_has_user_avatar_column': 'user_avatar' in user_columns,
                'users_has_user_verified_column': 'user_verified' in user_columns,
                'login_status': login_resp.status_code,
                'profile_default_avatar_rendered_before_upload': profile_has_default,
                'first_upload_status': upload_1.status_code,
                'first_avatar_file_exists': old_avatar_exists,
                'second_upload_status': upload_2.status_code,
                'avatar_replaced_with_new_filename': bool(old_avatar_name and new_avatar_name and old_avatar_name != new_avatar_name),
                'second_avatar_file_exists': new_avatar_exists,
                'old_avatar_deleted_on_replace': not old_avatar_exists_after_replace,
                'profile_shows_uploaded_avatar': profile_has_uploaded,
                'property_details_shows_owner_avatar': property_has_owner_avatar,
                'my_listings_shows_avatar': listings_has_avatar,
            }
        finally:
            try:
                if old_avatar_name:
                    old_path = os.path.join(avatar_dir, old_avatar_name)
                    if os.path.exists(old_path):
                        os.remove(old_path)
                if new_avatar_name:
                    new_path = os.path.join(avatar_dir, new_avatar_name)
                    if os.path.exists(new_path):
                        os.remove(new_path)
            except Exception:
                pass

            db.session.execute(text('DELETE FROM property WHERE prop_id = :pid'), {'pid': prop.prop_id})
            db.session.execute(text('DELETE FROM password_reset_tokens WHERE user_id = :uid'), {'uid': user.user_id})
            db.session.execute(text('DELETE FROM users WHERE user_id = :uid'), {'uid': user.user_id})
            db.session.commit()


if __name__ == '__main__':
    print(json.dumps(run_verification(), indent=2, default=str))

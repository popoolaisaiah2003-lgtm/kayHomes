import json
import os
import secrets
import sys

from werkzeug.security import generate_password_hash

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from pkg import app, initialize_database
from pkg.models import Admin, Category, Property, User, db


def _login_user_session(client, user):
    with client.session_transaction() as session_data:
        session_data['user_id'] = user.user_id
        session_data['user_name'] = user.user_fname
        session_data['theme'] = getattr(user, 'theme', 'light') or 'light'


def _login_admin_session(client, admin):
    with client.session_transaction() as session_data:
        session_data['admin_id'] = admin.admin_id
        session_data['admin_username'] = admin.username
        session_data['admin_name'] = admin.first_name or admin.username
        session_data['admin_csrf_token'] = 'smoke-test-token'


def run_smoke_test():
    initialize_database()
    results = {}

    with app.app_context():
        suffix = secrets.token_hex(4)
        category = Category.query.filter_by(cat_name=f'Polling Smoke {suffix}').first()
        if not category:
            category = Category(cat_name=f'Polling Smoke {suffix}', cat_desc='Smoke test category')
            db.session.add(category)
            db.session.commit()

        user_one = User(
            user_fname='Polling',
            user_lname='Owner',
            user_email=f'polling.owner.{suffix}@example.com',
            user_phone='08000000001',
            user_pwd=generate_password_hash('Password123!'),
            theme='light',
        )
        user_two = User(
            user_fname='Polling',
            user_lname='Viewer',
            user_email=f'polling.viewer.{suffix}@example.com',
            user_phone='08000000002',
            user_pwd=generate_password_hash('Password123!'),
            theme='light',
        )
        admin = Admin(
            first_name='Polling',
            last_name='Admin',
            username=f'polling_admin_{suffix}',
            email=f'polling.admin.{suffix}@example.com',
            phone='08000000003',
            password=generate_password_hash('Password123!'),
            role='admin',
            status='Active',
        )
        db.session.add_all([user_one, user_two, admin])
        db.session.commit()

        property_item = Property(
            prop_title=f'Polling Smoke Property {suffix}',
            prop_type=category.cat_name,
            listing_type='Rent',
            prop_desc='Smoke test property description',
            prop_price='500000',
            prop_location='Ikeja',
            prop_state='Lagos',
            prop_lga='Ikeja',
            prop_address='123 Smoke Test Avenue',
            prop_userid=user_one.user_id,
            category_id=category.cat_id,
        )
        db.session.add(property_item)
        db.session.commit()

        db.session.execute(
            db.text(
                '''
                INSERT INTO messages (property_id, sender_id, receiver_id, message, is_read, created_at)
                VALUES (:pid, :sender, :receiver, :message, 0, NOW())
                '''
            ),
            {
                'pid': property_item.prop_id,
                'sender': user_two.user_id,
                'receiver': user_one.user_id,
                'message': 'Initial smoke test message',
            },
        )
        db.session.commit()

        owner_client = app.test_client()
        _login_user_session(owner_client, user_one)

        profile_page = owner_client.get('/profile/')
        properties_page = owner_client.get('/properties/')
        listings_page = owner_client.get('/my-listings/')
        detail_page = owner_client.get(f'/property/{property_item.prop_id}')
        chat_page = owner_client.get(f'/chat/{property_item.prop_id}/{user_two.user_id}')

        results['profile_page'] = {
            'status': profile_page.status_code,
            'has_poll_hook': 'data-profile-stats-url' in profile_page.get_data(as_text=True),
        }
        results['properties_page'] = {
            'status': properties_page.status_code,
            'has_poll_hook': 'data-properties-feed' in properties_page.get_data(as_text=True),
        }
        results['my_listings_page'] = {
            'status': listings_page.status_code,
            'has_poll_hook': 'data-my-listings-feed' in listings_page.get_data(as_text=True),
        }
        results['property_detail_page'] = {
            'status': detail_page.status_code,
            'has_poll_hook': 'data-property-detail' in detail_page.get_data(as_text=True),
        }
        results['chat_page'] = {
            'status': chat_page.status_code,
            'has_poll_hook': 'data-chat-page' in chat_page.get_data(as_text=True),
        }

        unread_payload = owner_client.get('/api/messages/unread-count').get_json()
        profile_payload = owner_client.get('/api/profile/stats').get_json()
        listings_payload = owner_client.get('/api/my-listings/updates').get_json()
        properties_payload = owner_client.get('/api/properties/updates?before_id=0').get_json()
        detail_payload = owner_client.get(f'/api/property/{property_item.prop_id}/details').get_json()
        initial_chat_payload = owner_client.get(f'/api/chat/{property_item.prop_id}/{user_two.user_id}/messages?after_id=0').get_json()
        send_payload = owner_client.post(
            f'/api/chat/{property_item.prop_id}/{user_two.user_id}/send',
            data={'message': 'Reply from owner'},
        ).get_json()

        viewer_client = app.test_client()
        _login_user_session(viewer_client, user_two)
        viewer_chat_payload = viewer_client.get(
            f'/api/chat/{property_item.prop_id}/{user_one.user_id}/messages?after_id=1'
        ).get_json()

        admin_client = app.test_client()
        _login_admin_session(admin_client, admin)
        admin_page = admin_client.get('/admin/')
        admin_stats = admin_client.get('/api/admin/dashboard/stats').get_json()

        results['api'] = {
            'unread_count': unread_payload,
            'profile_stats': profile_payload,
            'my_listings_count': len(listings_payload.get('listings', [])),
            'properties_updates_count': len(properties_payload.get('properties', [])),
            'detail_has_images_key': 'images' in detail_payload,
            'chat_initial_count': len(initial_chat_payload.get('messages', [])),
            'chat_send_success': bool(send_payload.get('success')),
            'viewer_received_reply_count': len(viewer_chat_payload.get('messages', [])),
            'admin_stats_keys': sorted(admin_stats.keys()),
        }
        results['admin_page'] = {
            'status': admin_page.status_code,
            'has_poll_hook': 'data-admin-stats-url' in admin_page.get_data(as_text=True),
        }

        db.session.execute(db.text('DELETE FROM favorites WHERE fav_propid = :pid'), {'pid': property_item.prop_id})
        db.session.execute(db.text('DELETE FROM messages WHERE property_id = :pid'), {'pid': property_item.prop_id})
        db.session.delete(property_item)
        db.session.delete(admin)
        db.session.delete(user_two)
        db.session.delete(user_one)
        db.session.delete(category)
        db.session.commit()

    return results


if __name__ == '__main__':
    print(json.dumps(run_smoke_test(), indent=2, default=str))

import io
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone

from sqlalchemy import text
from werkzeug.security import generate_password_hash

from pkg import app, db, initialize_database
from pkg.models import Category, Property, User
from pkg.user_routes import delete_image_file, get_property_image_columns, get_property_images


def _extract_csrf_token(html_text):
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html_text)
    if not match:
        raise RuntimeError('CSRF token not found in post-property form.')
    return match.group(1)


def _png_bytes(seed):
    # Minimal valid PNG byte signature plus seed to keep each payload unique.
    return b'\x89PNG\r\n\x1a\n' + f'kayhomes-audit-{seed}'.encode('utf-8')


def _first_or_none(items):
    return items[0] if items else None


def _cleanup_property(property_id):
    image_rows = get_property_images(property_id)
    image_cols = get_property_image_columns()

    try:
        db.session.execute(text('DELETE FROM favorites WHERE fav_propid = :pid'), {'pid': property_id})
        db.session.execute(text('DELETE FROM inquiries WHERE inqu_propid = :pid'), {'pid': property_id})
        if image_cols and image_cols.get('property_id'):
            db.session.execute(
                text(f"DELETE FROM property_image WHERE {image_cols['property_id']} = :pid"),
                {'pid': property_id},
            )
        db.session.execute(text('DELETE FROM messages WHERE property_id = :pid'), {'pid': property_id})
        db.session.execute(text('DELETE FROM property WHERE prop_id = :pid'), {'pid': property_id})
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise

    for row in image_rows:
        delete_image_file(row.get('image_path'))


def run_runtime_audit():
    results = {
        'environment': {},
        'config': {},
        'schema': {},
        'runtime': {},
        'repairs': [],
    }

    app.config['TESTING'] = True
    app.config['MAIL_SUPPRESS_SEND'] = True

    with app.app_context():
        initialize_database()

        results['environment']['python'] = sys.executable
        results['config'] = {
            'database_uri': app.config.get('SQLALCHEMY_DATABASE_URI'),
            'secret_key_set': bool(app.config.get('SECRET_KEY')),
            'upload_folder': app.config.get('UPLOAD_FOLDER'),
            'mail_server': app.config.get('MAIL_SERVER'),
            'mail_port': app.config.get('MAIL_PORT'),
        }

        required_tables = [
            'property',
            'property_image',
            'categories',
            'users',
            'favorites',
            'messages',
            'inquiries',
            'contact_messages',
            'admin',
        ]

        inspector = db.inspect(db.engine)
        existing_tables = set(inspector.get_table_names())
        results['schema']['existing_tables'] = sorted(existing_tables)
        results['schema']['required_tables_present'] = {
            table_name: table_name in existing_tables for table_name in required_tables
        }
        results['schema']['table_columns'] = {}
        for table_name in required_tables:
            if table_name in existing_tables:
                results['schema']['table_columns'][table_name] = [
                    col['name'] for col in inspector.get_columns(table_name)
                ]

        category = Category.query.order_by(Category.cat_id.asc()).first()
        if not category:
            category = Category(cat_name='Runtime Audit Category', cat_desc='Created by runtime audit')
            db.session.add(category)
            db.session.commit()
            results['repairs'].append('Created fallback category for property posting audit.')

        run_id = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
        email = f'runtime.audit.{run_id}@example.com'
        password = 'AuditPass123!'

        user = User(
            user_fname='Runtime',
            user_lname='Audit',
            user_email=email,
            user_phone='08000000000',
            user_pwd=generate_password_hash(password),
        )
        db.session.add(user)
        db.session.commit()

        created_property_id = None
        try:
            client = app.test_client()

            login_response = client.post(
                '/login/',
                data={'email': email, 'password': password},
                follow_redirects=False,
            )
            results['runtime']['login_status'] = login_response.status_code
            if login_response.status_code not in (302, 303):
                raise RuntimeError(f'Login failed with status {login_response.status_code}')

            get_form_response = client.get('/post-property', follow_redirects=False)
            if get_form_response.status_code != 200:
                raise RuntimeError(f'post-property form load failed with status {get_form_response.status_code}')

            html = get_form_response.get_data(as_text=True)
            if 'name="images"' not in html or 'multiple' not in html:
                raise RuntimeError('Post-property form missing image input with name="images" and multiple attribute.')
            csrf_token = _extract_csrf_token(html)
            results['runtime']['post_form_has_images_multiple'] = True

            property_title = f'Runtime Audit Property {run_id}'
            data = {
                'csrf_token': csrf_token,
                'prop_title': property_title,
                'category_id': str(category.cat_id),
                'listing_type': 'Rent',
                'prop_desc': 'Runtime audit property description',
                'prop_price': '500000',
                'prop_location': 'Ikeja',
                'prop_state': 'Lagos',
                'prop_lga': 'Ikeja',
                'prop_address': '123 Audit Street',
                'images': [
                    (io.BytesIO(_png_bytes(i)), f'audit_{i}.png') for i in range(1, 6)
                ],
            }

            create_response = client.post(
                '/post-property',
                data=data,
                content_type='multipart/form-data',
                follow_redirects=False,
            )
            results['runtime']['create_property_status'] = create_response.status_code
            if create_response.status_code not in (302, 303):
                raise RuntimeError(f'Create property failed with status {create_response.status_code}')

            created_property = Property.query.filter_by(prop_title=property_title, prop_userid=user.user_id).order_by(Property.prop_id.desc()).first()
            if not created_property:
                raise RuntimeError('Property row was not created in database.')
            created_property_id = created_property.prop_id
            results['runtime']['created_property_id'] = created_property_id

            created_images = get_property_images(created_property_id)
            results['runtime']['initial_image_count'] = len(created_images)
            if len(created_images) != 5:
                raise RuntimeError(f'Expected 5 uploaded images, found {len(created_images)}')

            upload_folder = app.config['UPLOAD_FOLDER']
            missing_files = []
            for image_row in created_images:
                image_path = image_row.get('image_path')
                if not image_path:
                    missing_files.append('<empty-image-path>')
                    continue
                full_path = os.path.join(upload_folder, image_path)
                if not os.path.exists(full_path):
                    missing_files.append(image_path)
            if missing_files:
                raise RuntimeError(f'Uploaded image files missing on disk: {missing_files}')

            detail_response = client.get(f'/property/{created_property_id}', follow_redirects=False)
            if detail_response.status_code != 200:
                raise RuntimeError(f'Property detail failed with status {detail_response.status_code}')

            detail_html = detail_response.get_data(as_text=True)
            first_image = _first_or_none(created_images)
            if not first_image or first_image.get('image_path') not in detail_html:
                raise RuntimeError('Cover image not rendered on property detail page.')
            for image_row in created_images[1:]:
                if image_row.get('image_path') not in detail_html:
                    raise RuntimeError(f"Gallery image {image_row.get('image_path')} not rendered on property detail page.")
            results['runtime']['detail_gallery_verified'] = True

            edit_form_response = client.get(f'/post-property/{created_property_id}', follow_redirects=False)
            if edit_form_response.status_code != 200:
                raise RuntimeError(f'Edit form load failed with status {edit_form_response.status_code}')

            edit_csrf = _extract_csrf_token(edit_form_response.get_data(as_text=True))
            edit_response = client.post(
                f'/post-property/{created_property_id}',
                data={
                    'csrf_token': edit_csrf,
                    'prop_title': f'{property_title} Updated',
                    'category_id': str(category.cat_id),
                    'listing_type': 'Short Let',
                    'prop_desc': 'Updated runtime audit description',
                    'prop_price': '650000',
                    'prop_location': 'Lekki',
                    'prop_state': 'Lagos',
                    'prop_lga': 'Eti-Osa',
                    'prop_address': '456 Audit Avenue',
                    'images': [],
                },
                content_type='multipart/form-data',
                follow_redirects=False,
            )
            results['runtime']['edit_property_status'] = edit_response.status_code
            if edit_response.status_code not in (302, 303):
                raise RuntimeError(f'Edit property failed with status {edit_response.status_code}')

            created_images = get_property_images(created_property_id)
            image_to_delete = _first_or_none(created_images)
            if not image_to_delete or not image_to_delete.get('image_id'):
                raise RuntimeError('Unable to resolve deletable image_id for delete test.')

            delete_target = image_to_delete.get('image_path')
            delete_file_path = os.path.join(upload_folder, delete_target)
            delete_response = client.post(
                f"/listing/{created_property_id}/image/{image_to_delete['image_id']}/delete",
                follow_redirects=False,
            )
            results['runtime']['delete_image_status'] = delete_response.status_code
            if delete_response.status_code not in (302, 303):
                raise RuntimeError(f'Delete image failed with status {delete_response.status_code}')

            images_after_delete = get_property_images(created_property_id)
            results['runtime']['image_count_after_delete'] = len(images_after_delete)
            if len(images_after_delete) != 4:
                raise RuntimeError(f'Expected 4 images after delete, found {len(images_after_delete)}')
            if os.path.exists(delete_file_path):
                raise RuntimeError('Deleted image file still exists on disk.')

            reupload_form_response = client.get(f'/post-property/{created_property_id}', follow_redirects=False)
            if reupload_form_response.status_code != 200:
                raise RuntimeError(f'Reupload edit form failed with status {reupload_form_response.status_code}')

            reupload_csrf = _extract_csrf_token(reupload_form_response.get_data(as_text=True))
            reupload_response = client.post(
                f'/post-property/{created_property_id}',
                data={
                    'csrf_token': reupload_csrf,
                    'prop_title': f'{property_title} Updated Again',
                    'category_id': str(category.cat_id),
                    'listing_type': 'Rent',
                    'prop_desc': 'Reupload runtime audit description',
                    'prop_price': '700000',
                    'prop_location': 'Victoria Island',
                    'prop_state': 'Lagos',
                    'prop_lga': 'Eti-Osa',
                    'prop_address': '789 Audit Boulevard',
                    'images': [(io.BytesIO(_png_bytes(99)), 'audit_reupload.png')],
                },
                content_type='multipart/form-data',
                follow_redirects=False,
            )
            results['runtime']['reupload_image_status'] = reupload_response.status_code
            if reupload_response.status_code not in (302, 303):
                raise RuntimeError(f'Reupload image failed with status {reupload_response.status_code}')

            images_after_reupload = get_property_images(created_property_id)
            results['runtime']['image_count_after_reupload'] = len(images_after_reupload)
            if len(images_after_reupload) != 5:
                raise RuntimeError(f'Expected 5 images after reupload, found {len(images_after_reupload)}')

            newest_image = images_after_reupload[-1]
            newest_path = newest_image.get('image_path')
            if not newest_path or not os.path.exists(os.path.join(upload_folder, newest_path)):
                raise RuntimeError('Reuploaded image file not found on disk.')

            results['runtime']['full_lifecycle_passed'] = True
        finally:
            if created_property_id:
                _cleanup_property(created_property_id)
            db.session.delete(user)
            db.session.commit()

    return results


if __name__ == '__main__':
    try:
        output = run_runtime_audit()
        print(json.dumps(output, indent=2, default=str))
    except Exception:
        traceback.print_exc()
        sys.exit(1)

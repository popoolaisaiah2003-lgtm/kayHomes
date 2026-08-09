import json
import re

from sqlalchemy import text

from pkg import app, db, initialize_database


def _extract_csrf_token(html_text):
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', html_text)
    if not match:
        return None
    return match.group(1)


def run_checks():
    app.config['TESTING'] = True
    app.config['MAIL_SUPPRESS_SEND'] = True

    with app.app_context():
        initialize_database()

        prop_row = db.session.execute(
            text(
                '''SELECT prop_id, prop_userid, prop_title, prop_desc, prop_price, prop_location,
                          prop_state, prop_lga, prop_address, listing_type, category_id
                   FROM property
                   WHERE prop_userid IS NOT NULL
                   ORDER BY prop_id DESC
                   LIMIT 1'''
            )
        ).mappings().first()

        if not prop_row:
            return {'error': 'No property with owner was found for route validation.'}

        owner_id = int(prop_row['prop_userid'])
        property_id = int(prop_row['prop_id'])
        original_lga = prop_row.get('prop_lga')

        category_id = prop_row.get('category_id')
        if not category_id:
            category_row = db.session.execute(
                text('SELECT cat_id FROM categories ORDER BY cat_id ASC LIMIT 1')
            ).mappings().first()
            category_id = category_row['cat_id'] if category_row else None

        client = app.test_client()
        with client.session_transaction() as sess:
            sess['user_id'] = owner_id
            sess['user_name'] = 'RouteCheckUser'

        post_property_resp = client.get('/post-property', follow_redirects=False)
        edit_property_resp = client.get(f'/edit-property/{property_id}', follow_redirects=True)
        property_detail_resp = client.get(f'/property/{property_id}', follow_redirects=False)

        form_html = post_property_resp.get_data(as_text=True)
        edit_html = edit_property_resp.get_data(as_text=True)

        csrf_token = _extract_csrf_token(edit_html)
        validation_post_status = None
        lga_unchanged = None
        if csrf_token and category_id:
            post_resp = client.post(
                f'/post-property/{property_id}',
                data={
                    'csrf_token': csrf_token,
                    'prop_title': prop_row['prop_title'] or 'Validation Test Property',
                    'category_id': str(category_id),
                    'listing_type': prop_row['listing_type'] or 'Rent',
                    'prop_desc': prop_row['prop_desc'] or 'Validation test description',
                    'prop_price': str(prop_row['prop_price'] or '100000'),
                    'prop_location': prop_row['prop_location'] or 'Ikeja',
                    'prop_state': prop_row['prop_state'] or 'Lagos',
                    'prop_lga': '',
                    'prop_address': prop_row['prop_address'] or 'Validation Address',
                },
                follow_redirects=False,
            )
            validation_post_status = post_resp.status_code
            refreshed_lga = db.session.execute(
                text('SELECT prop_lga FROM property WHERE prop_id = :pid'),
                {'pid': property_id},
            ).scalar()
            lga_unchanged = (refreshed_lga == original_lga)

        return {
            'property_id_used_for_route_checks': property_id,
            'post_property_status': post_property_resp.status_code,
            'edit_property_status_follow_redirects': edit_property_resp.status_code,
            'property_detail_status': property_detail_resp.status_code,
            'form_has_lga_error_container': 'id="lgaValidationError"' in form_html,
            'form_has_submit_validation_message': 'Please select a local government area for the selected state.' in form_html,
            'server_validation_post_status': validation_post_status,
            'server_validation_preserved_existing_lga': lga_unchanged,
        }


if __name__ == '__main__':
    print(json.dumps(run_checks(), indent=2, default=str))

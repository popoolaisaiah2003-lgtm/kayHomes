import json
import re
from datetime import datetime

from sqlalchemy import text
from werkzeug.security import generate_password_hash

from pkg import app, db, initialize_database
from pkg.models import User


def run_check():
    app.config['TESTING'] = True
    app.config['MAIL_SUPPRESS_SEND'] = True

    with app.app_context():
        initialize_database()

        email = f"form.verify.{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}@example.com"
        user = User(
            user_fname='Form',
            user_lname='Verify',
            user_email=email,
            user_phone='08000000002',
            user_pwd=generate_password_hash('FormVerify123!'),
        )
        db.session.add(user)
        db.session.commit()

        try:
            client = app.test_client()
            login_resp = client.post('/login/', data={'email': email, 'password': 'FormVerify123!'}, follow_redirects=False)
            form_resp = client.get('/post-property', follow_redirects=False)
            html = form_resp.get_data(as_text=True)

            state_select = html.split('id="prop_state"', 1)[1].split('</select>', 1)[0]
            options = re.findall(r'<option value="([^"]+)"[^>]*>([^<]+)</option>', state_select)
            state_values = [opt[0].strip() for opt in options if opt[0].strip()]

            # `state_lga_map` should be emitted as JS object from backend data.
            has_lga_map = 'const lgaMap = ' in html
            has_lagos_lga = 'Eti-Osa' in html
            has_abuja_lga = 'Abuja Municipal' in html

            return {
                'login_status': login_resp.status_code,
                'post_property_status': form_resp.status_code,
                'state_option_count': len(state_values),
                'contains_lagos': 'Lagos' in state_values,
                'contains_abuja_fct': 'Abuja (FCT)' in state_values,
                'contains_kano': 'Kano' in state_values,
                'has_lga_map_js': has_lga_map,
                'has_lagos_lga_in_js': has_lagos_lga,
                'has_abuja_lga_in_js': has_abuja_lga,
            }
        finally:
            db.session.execute(text('DELETE FROM users WHERE user_id = :uid'), {'uid': user.user_id})
            db.session.commit()


if __name__ == '__main__':
    print(json.dumps(run_check(), indent=2))

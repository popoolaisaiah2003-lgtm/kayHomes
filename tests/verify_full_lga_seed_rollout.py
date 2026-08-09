import json
import re
from datetime import datetime

from sqlalchemy import text
from werkzeug.security import generate_password_hash

from pkg import app, db, initialize_database
from pkg.models import User


def run_verification():
    app.config['TESTING'] = True
    app.config['MAIL_SUPPRESS_SEND'] = True

    with app.app_context():
        initialize_database()

        total_states = int(db.session.execute(text('SELECT COUNT(*) FROM state')).scalar() or 0)
        total_lgas = int(db.session.execute(text('SELECT COUNT(*) FROM lga')).scalar() or 0)

        zero_rows = db.session.execute(
            text(
                '''SELECT s.state_name, COUNT(l.lga_id) AS lga_count
                   FROM state s
                   LEFT JOIN lga l ON l.lga_stateid = s.state_id
                   GROUP BY s.state_id, s.state_name
                   HAVING COUNT(l.lga_id) = 0
                   ORDER BY s.state_name'''
            )
        ).mappings().all()
        zero_states = [r['state_name'] for r in zero_rows]

        fk_rows = db.session.execute(
            text(
                '''SELECT CONSTRAINT_NAME, TABLE_NAME, COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
                   FROM information_schema.KEY_COLUMN_USAGE
                   WHERE TABLE_SCHEMA = DATABASE()
                     AND TABLE_NAME='lga'
                     AND REFERENCED_TABLE_NAME IS NOT NULL'''
            )
        ).mappings().all()

        email = f"lga.verify.{datetime.utcnow().strftime('%Y%m%d%H%M%S%f')}@example.com"
        user = User(
            user_fname='Lga',
            user_lname='Verify',
            user_email=email,
            user_phone='08000000009',
            user_pwd=generate_password_hash('LgaVerify123!'),
        )
        db.session.add(user)
        db.session.commit()

        try:
            client = app.test_client()
            client.post('/login/', data={'email': email, 'password': 'LgaVerify123!'}, follow_redirects=False)
            resp = client.get('/post-property', follow_redirects=False)
            html = resp.get_data(as_text=True)

            state_select = html.split('id="prop_state"', 1)[1].split('</select>', 1)[0]
            state_options = re.findall(r'<option value="([^"]+)"[^>]*>[^<]+</option>', state_select)
            option_states = [s for s in state_options if s.strip()]

            map_match = re.search(r'const lgaMap = (\{.*?\});', html, flags=re.S)
            lga_map_keys = []
            if map_match:
                lga_map_keys = re.findall(r'"([^"]+)"\s*:', map_match.group(1))

            return {
                'total_states': total_states,
                'total_lgas': total_lgas,
                'states_with_zero_lgas': zero_states,
                'lga_fk': [dict(r) for r in fk_rows],
                'post_property_status': resp.status_code,
                'post_property_state_options': len(option_states),
                'post_property_lga_map_keys': len(set(lga_map_keys)),
                'dropdown_dynamic_all_states': (
                    resp.status_code == 200
                    and len(option_states) == total_states
                    and len(set(lga_map_keys)) == total_states
                ),
            }
        finally:
            db.session.execute(text('DELETE FROM users WHERE user_id = :uid'), {'uid': user.user_id})
            db.session.commit()


if __name__ == '__main__':
    print(json.dumps(run_verification(), indent=2, default=str))

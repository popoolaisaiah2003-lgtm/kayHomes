import json
import sys
from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash

try:
    from pkg import app, db
    from pkg.models import User, PasswordResetToken
except Exception as e:
    print(f"Error importing app: {e}")
    sys.exit(1)

# Helper to verify reset flow
def run_verification():
    results = {}
    
    # 1. Use Flask app context
    app.config['WTF_CSRF_ENABLED'] = False
    app.config['TESTING'] = True
    
    with app.app_context():
        # Ensure test user
        email = "test_reset_verify@example.com"
        # delete any existing
        User.query.filter_by(user_email=email).delete()
        PasswordResetToken.query.filter(PasswordResetToken.user_id.notin_(db.session.query(User.user_id))).delete(synchronize_session=False)
        db.session.commit()
        
        # create valid user
        initial_pwd = "old_password_123"
        user = User(
            user_fname="Test",
            user_lname="User",
            user_email=email,
            user_pwd=generate_password_hash(initial_pwd)
        )
        db.session.add(user)
        db.session.commit()
        
        initial_hash = user.user_pwd
        results['user_created'] = True
        
        # 2. Use test client
        client = app.test_client()
        
        # 3. Post valid email to /forgot-password
        resp1 = client.post('/forgot-password', data={'email': email})
        results['forgot_password_status'] = resp1.status_code
        
        # 4. Verify token exists and is unused
        token_row = PasswordResetToken.query.filter_by(user_id=user.user_id).order_by(PasswordResetToken.id.desc()).first()
        if token_row:
            results['token_exists'] = True
            results['token_used_initially'] = token_row.used
            token_value = token_row.token
        else:
            results['token_exists'] = False
            token_value = None
            
        # 5. Reset password with token
        if token_value:
            resp2 = client.post(f'/reset-password/{token_value}', data={
                'password': 'new_secpassword_987',
                'confirm_password': 'new_secpassword_987'
            })
            results['reset_password_status'] = resp2.status_code
            
            # Refresh user and token from DB
            db.session.refresh(user)
            db.session.refresh(token_row)
            
            results['token_used_after'] = token_row.used
            results['hash_changed'] = (user.user_pwd != initial_hash)
            results['new_password_valid'] = check_password_hash(user.user_pwd, 'new_secpassword_987')
            
        # 6. Cleanup
        PasswordResetToken.query.filter_by(user_id=user.user_id).delete()
        db.session.delete(user)
        db.session.commit()
        results['cleanup_done'] = True
        
    return results

if __name__ == '__main__':
    res = run_verification()
    print("BEGIN")
    print(json.dumps(res, indent=2))
    print("END")

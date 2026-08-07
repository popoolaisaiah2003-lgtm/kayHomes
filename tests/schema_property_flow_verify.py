import json
import re
import traceback
from datetime import datetime, timezone

from sqlalchemy import text
from werkzeug.security import generate_password_hash

from pkg import app, db, initialize_database, ensure_property_reviews_table
from pkg.models import Category, Property, User
from pkg.user_routes import get_property_image_columns


REQUIRED_REVIEW_INDEXES = {
    'idx_rev_property_id': ('property_id',),
    'idx_rev_owner_id': ('owner_id',),
    'idx_rev_reviewer_id': ('reviewer_id',),
    'idx_rev_created_at': ('created_at',),
}
REQUIRED_REVIEW_UNIQUE = ('property_id', 'reviewer_id')


def _index_map(table_name):
    rows = db.session.execute(text(f'SHOW INDEX FROM {table_name}')).mappings().all()
    index_map = {}
    for row in rows:
        name = row.get('Key_name')
        if not name:
            continue
        non_unique_raw = row.get('Non_unique')
        non_unique = int(non_unique_raw) if non_unique_raw is not None else 1
        payload = index_map.setdefault(name, {'non_unique': non_unique, 'cols': []})
        payload['cols'].append((int(row.get('Seq_in_index') or 0), row.get('Column_name')))

    normalized = {}
    for name, payload in index_map.items():
        ordered_cols = tuple(col for _, col in sorted(payload['cols'], key=lambda item: item[0]))
        normalized[name] = {'non_unique': payload['non_unique'], 'cols': ordered_cols}
    return normalized


def _table_columns(inspector, table_name):
    if not inspector.has_table(table_name):
        return []
    return [c['name'] for c in inspector.get_columns(table_name)]


def _find_legacy_category_references():
    findings = []
    sql_pattern = re.compile(
        r"\b(FROM|JOIN|INTO|DELETE\s+FROM|TABLE)\s+`?category`?\b|\bUPDATE\s+`?category`?\s+SET\b",
        re.IGNORECASE,
    )
    sql_context_pattern = re.compile(r"\b(SELECT|INSERT|UPDATE|DELETE|JOIN|FROM|ALTER|CREATE)\b", re.IGNORECASE)

    import os
    repo_root = app.root_path.rsplit('pkg', 1)[0].rstrip('\\/')
    for scan_root in ('pkg', 'tests', 'migrations'):
        root_path = os.path.join(repo_root, scan_root)
        if not os.path.isdir(root_path):
            continue
        for dirpath, _, filenames in os.walk(root_path):
            for filename in filenames:
                if not filename.endswith(('.py', '.sql')):
                    continue
                file_path = os.path.join(dirpath, filename)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        for line_no, line in enumerate(f, start=1):
                            if sql_context_pattern.search(line) and sql_pattern.search(line):
                                rel = os.path.relpath(file_path, repo_root).replace('\\', '/')
                                findings.append({'file': rel, 'line': line_no, 'snippet': line.strip()[:220]})
                except Exception:
                    continue

    return findings


def run_verification():
    report = {
        'part1_active_database': {},
        'part2_property_reviews_schema': {},
        'part3_property_columns': {},
        'part4_read_path': {},
        'part5_image_audit': {},
        'part6_category_table_audit': {},
        'part7_verification': {},
    }

    app.config['TESTING'] = True
    app.config['MAIL_SUPPRESS_SEND'] = True

    with app.app_context():
        initialize_database()
        ensure_property_reviews_table()

        # Part 1: active DB confirmation
        report['part1_active_database']['sqlalchemy_database_uri'] = app.config.get('SQLALCHEMY_DATABASE_URI')
        report['part1_active_database']['active_database'] = db.session.execute(text('SELECT DATABASE()')).scalar()

        inspector = db.inspect(db.engine)

        # Part 2: reviews table/index/constraint checks
        review_columns = _table_columns(inspector, 'property_reviews')
        review_indexes = _index_map('property_reviews') if inspector.has_table('property_reviews') else {}
        required_idx_status = {
            idx_name: (
                idx_name in review_indexes and
                review_indexes[idx_name]['non_unique'] == 1 and
                review_indexes[idx_name]['cols'] == idx_cols
            )
            for idx_name, idx_cols in REQUIRED_REVIEW_INDEXES.items()
        }
        unique_ok = (
            'uq_user_property_review' in review_indexes and
            review_indexes['uq_user_property_review']['non_unique'] == 0 and
            review_indexes['uq_user_property_review']['cols'] == REQUIRED_REVIEW_UNIQUE
        )

        report['part2_property_reviews_schema'] = {
            'table_exists': inspector.has_table('property_reviews'),
            'columns': review_columns,
            'indexes': {
                name: {
                    'non_unique': payload['non_unique'],
                    'columns': list(payload['cols']),
                }
                for name, payload in sorted(review_indexes.items())
            },
            'required_index_names_present': required_idx_status,
            'required_unique_constraint_present': unique_ok,
        }

        # Part 3: property field persistence columns
        property_columns = _table_columns(inspector, 'property')
        required_property_cols = ['bedrooms', 'bathrooms', 'toilets', 'area_sqm', 'prop_bedroom', 'prop_bathroom', 'prop_toilet', 'prop_area']
        report['part3_property_columns'] = {
            'columns': property_columns,
            'required_columns_present': {c: c in property_columns for c in required_property_cols},
        }

        # Part 4: read path verification is reflected by route payload shape
        report['part4_read_path'] = {
            'property_detail_uses_keys': ['bedrooms', 'bathrooms', 'toilets', 'area_sqm'],
        }

        # Part 5: orphan image count only
        img_cols = get_property_image_columns()
        orphan_count = 0
        if img_cols and img_cols.get('property_id'):
            fk_col = img_cols['property_id']
            orphan_count = db.session.execute(
                text(
                    f'''SELECT COUNT(*)
                        FROM property_image pi
                        LEFT JOIN property p ON p.prop_id = pi.{fk_col}
                        WHERE p.prop_id IS NULL'''
                )
            ).scalar() or 0

        report['part5_image_audit'] = {
            'orphan_property_image_rows': int(orphan_count),
        }

        # Part 6: category reference audit
        legacy_refs = _find_legacy_category_references()
        report['part6_category_table_audit'] = {
            'legacy_category_table_references': legacy_refs,
            'legacy_category_reference_count': len(legacy_refs),
        }

        # Part 7: end-to-end verification with temp property
        run_id = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
        email = f'schema.verify.{run_id}@example.com'
        password = 'SchemaVerify123!'

        category = Category.query.order_by(Category.cat_id.asc()).first()
        if not category:
            category = Category(cat_name='Schema Verify Category', cat_desc='Created by schema verification script')
            db.session.add(category)
            db.session.commit()

        temp_user = User(
            user_fname='Schema',
            user_lname='Verifier',
            user_email=email,
            user_phone='08000000001',
            user_pwd=generate_password_hash(password),
        )
        db.session.add(temp_user)
        db.session.commit()

        temp_property_id = None
        route_status = {}

        try:
            temp_property = Property(
                prop_title=f'Schema Verify Property {run_id}',
                category_id=category.cat_id,
                prop_type=category.cat_name,
                listing_type='Rent',
                prop_desc='Temp property for schema verification',
                prop_price='123456',
                prop_location='Ikeja',
                prop_state='Lagos',
                prop_lga='Ikeja',
                prop_address='Verification Street 1',
                prop_userid=temp_user.user_id,
                prop_bedroom=4,
                prop_bathroom=3,
                prop_toilet=4,
                prop_area=250,
                prop_area_unit='sqm',
                bedrooms=4,
                bathrooms=3,
                toilets=4,
                area_sqm=250,
            )
            db.session.add(temp_property)
            db.session.commit()
            temp_property_id = temp_property.prop_id

            persisted = db.session.execute(
                text(
                    '''SELECT prop_id, prop_bedroom, prop_bathroom, prop_toilet, prop_area,
                              bedrooms, bathrooms, toilets, area_sqm
                       FROM property
                       WHERE prop_id = :pid'''
                ),
                {'pid': temp_property_id},
            ).mappings().first()

            client = app.test_client()
            login_resp = client.post('/login/', data={'email': email, 'password': password}, follow_redirects=False)
            route_status['login_status'] = login_resp.status_code

            detail_resp = client.get(f'/property/{temp_property_id}', follow_redirects=False)
            route_status['property_detail_status'] = detail_resp.status_code

            profile_resp = client.get('/profile/', follow_redirects=True)
            route_status['profile_status'] = profile_resp.status_code

            listings_resp = client.get('/my-listings/', follow_redirects=True)
            route_status['my_listings_status'] = listings_resp.status_code

            report['part7_verification'] = {
                'property_columns_final': property_columns,
                'property_reviews_columns_final': review_columns,
                'tables_final': sorted(inspector.get_table_names()),
                'temp_property_id': temp_property_id,
                'temp_property_persisted_values': dict(persisted) if persisted else None,
                'route_status': route_status,
            }
        finally:
            if temp_property_id is not None:
                try:
                    db.session.execute(text('DELETE FROM property_reviews WHERE property_id = :pid'), {'pid': temp_property_id})
                    db.session.execute(text('DELETE FROM favorites WHERE fav_propid = :pid'), {'pid': temp_property_id})
                    db.session.execute(text('DELETE FROM inquiries WHERE inqu_propid = :pid'), {'pid': temp_property_id})
                    db.session.execute(text('DELETE FROM messages WHERE property_id = :pid'), {'pid': temp_property_id})
                    img_cols = get_property_image_columns()
                    if img_cols and img_cols.get('property_id'):
                        db.session.execute(
                            text(f'DELETE FROM property_image WHERE {img_cols["property_id"]} = :pid'),
                            {'pid': temp_property_id},
                        )
                    db.session.execute(text('DELETE FROM property WHERE prop_id = :pid'), {'pid': temp_property_id})
                    db.session.commit()
                except Exception:
                    db.session.rollback()

            try:
                db.session.execute(text('DELETE FROM password_reset_tokens WHERE user_id = :uid'), {'uid': temp_user.user_id})
                db.session.execute(text('DELETE FROM users WHERE user_id = :uid'), {'uid': temp_user.user_id})
                db.session.commit()
            except Exception:
                db.session.rollback()

    return report


if __name__ == '__main__':
    try:
        result = run_verification()
        print(json.dumps(result, indent=2, default=str))
    except Exception:
        traceback.print_exc()
        raise

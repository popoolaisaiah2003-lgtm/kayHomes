from flask_sqlalchemy import SQLAlchemy
from sqlalchemy.orm import relationship, synonym

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = 'users'

    user_id = db.Column(db.Integer, primary_key=True)
    user_fname = db.Column(db.String(100), nullable=False)
    user_lname = db.Column(db.String(100), nullable=False)
    user_email = db.Column(db.String(120), unique=True, nullable=False)
    user_pwd = db.Column(db.String(255), nullable=False)
    user_phone = db.Column(db.String(20))
    user_avatar = db.Column(db.String(255), nullable=True)
    user_verified = db.Column(db.Boolean, nullable=False, default=False)
    theme = db.Column(db.String(20), nullable=False, default='light')
    user_regdate = db.Column(db.DateTime)

    password_reset_tokens = relationship('PasswordResetToken', back_populates='user', cascade='all, delete-orphan')


class PasswordResetToken(db.Model):
    __tablename__ = 'password_reset_tokens'

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=False, index=True)
    token = db.Column(db.String(255), nullable=False, unique=True, index=True)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=db.func.now())

    user = relationship('User', back_populates='password_reset_tokens')


class Admin(db.Model):
    __tablename__ = 'admin'

    admin_id = db.Column('adm_id', db.Integer, primary_key=True)
    first_name = db.Column(db.String(100), nullable=False, default='')
    last_name = db.Column(db.String(100), nullable=False, default='')
    username = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(20))
    password = db.Column(db.String(255), nullable=False)
    profile_image = db.Column(db.String(255))
    role = db.Column(db.String(50), nullable=False, default='admin')
    status = db.Column(db.String(20), nullable=False, default='Active')
    created_at = db.Column(db.DateTime, nullable=False, default=db.func.now())
    updated_at = db.Column(db.DateTime, nullable=False, default=db.func.now(), onupdate=db.func.now())

    adm_id = synonym('admin_id')
    adm_username = synonym('username')
    adm_user_name = synonym('username')
    adm_pwd = synonym('password')


class Category(db.Model):
    __tablename__ = 'categories'

    cat_id = db.Column(db.Integer, primary_key=True)
    cat_name = db.Column(db.String(100), nullable=False, unique=True)
    cat_desc = db.Column(db.Text)
    cat_date = db.Column(db.DateTime, nullable=False, default=db.func.now())

    properties = relationship('Property', back_populates='category')


class Property(db.Model):
    __tablename__ = 'property'

    prop_id = db.Column(db.Integer, primary_key=True)
    prop_title = db.Column(db.String(255), nullable=False)
    prop_type = db.Column(db.String(120), nullable=False)
    listing_type = db.Column(db.String(120))
    prop_status = db.Column(db.String(20), nullable=False, default='available')
    prop_views = db.Column(db.Integer, nullable=False, default=0)
    prop_desc = db.Column(db.Text, nullable=False)
    prop_price = db.Column(db.String(120), nullable=False)
    prop_location = db.Column(db.String(255), nullable=False)
    prop_state = db.Column(db.String(120), nullable=False)
    prop_lga = db.Column(db.String(120))
    prop_address = db.Column(db.String(255), nullable=False)
    prop_bedroom = db.Column(db.Integer, nullable=True)
    prop_bathroom = db.Column(db.Integer, nullable=True)
    prop_toilet = db.Column(db.Integer, nullable=True)
    prop_area = db.Column(db.Integer, nullable=True)
    prop_area_unit = db.Column(db.String(20), nullable=True, default='sqm')
    bedrooms = db.Column(db.Integer, nullable=True)
    bathrooms = db.Column(db.Integer, nullable=True)
    toilets = db.Column(db.Integer, nullable=True)
    area_sqm = db.Column(db.Integer, nullable=True)
    prop_userid = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    category_id = db.Column(db.Integer, db.ForeignKey('categories.cat_id', onupdate='CASCADE', ondelete='RESTRICT'))

    category = relationship('Category', back_populates='properties')


class Favorite(db.Model):
    __tablename__ = 'favorites'

    fav_id = db.Column(db.Integer, primary_key=True)
    fav_userid = db.Column(db.Integer, db.ForeignKey('users.user_id'))
    fav_propid = db.Column(db.Integer, db.ForeignKey('property.prop_id'))


class SavedSearch(db.Model):
    __tablename__ = 'saved_searches'

    search_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.user_id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    q = db.Column(db.String(255))
    state = db.Column(db.String(120))
    lga = db.Column(db.String(120))
    property_type = db.Column(db.String(120))
    bedrooms = db.Column(db.Integer)
    bathrooms = db.Column(db.Integer)
    min_price = db.Column(db.Integer)
    max_price = db.Column(db.Integer)
    furnished = db.Column(db.String(20))
    sort = db.Column(db.String(20))
    created_at = db.Column(db.DateTime, nullable=False, default=db.func.now())


class Notification(db.Model):
    __tablename__ = 'notifications'

    notification_id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.user_id', ondelete='CASCADE', onupdate='CASCADE'), nullable=False, index=True)
    type = db.Column(db.String(40), nullable=False)
    title = db.Column(db.String(150), nullable=False)
    message = db.Column(db.String(255), nullable=False)
    link = db.Column(db.String(255))
    is_read = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime, nullable=False, default=db.func.now(), index=True)


class ContactMessage(db.Model):
    __tablename__ = 'contact_messages'

    message_id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(20))
    subject = db.Column(db.String(150))
    message = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(20), nullable=False, default='Unread')
    created_at = db.Column(db.DateTime, nullable=False, default=db.func.now())
    updated_at = db.Column(db.DateTime, nullable=False, default=db.func.now(), onupdate=db.func.now())


class PropertyReview(db.Model):
    __tablename__ = 'property_reviews'
    __table_args__ = (
        db.UniqueConstraint('property_id', 'reviewer_id', name='uq_user_property_review'),
    )

    review_id = db.Column(db.Integer, primary_key=True)
    property_id = db.Column(db.Integer, db.ForeignKey('property.prop_id'), nullable=False, index=True)
    reviewer_id = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=False, index=True)
    owner_id = db.Column(db.Integer, db.ForeignKey('users.user_id'), nullable=False, index=True)
    rating = db.Column(db.Integer, nullable=False)
    review_text = db.Column(db.Text, nullable=True)
    review_tags = db.Column(db.String(255), nullable=True)
    is_visible = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=db.func.now(), index=True)

    property = relationship('Property', foreign_keys=[property_id])
    reviewer = relationship('User', foreign_keys=[reviewer_id])
    owner = relationship('User', foreign_keys=[owner_id])
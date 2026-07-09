from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


class User(db.Model):
    __tablename__ = 'users'

    user_id = db.Column(db.Integer, primary_key=True)
    user_fname = db.Column(db.String(100), nullable=False)
    user_lname = db.Column(db.String(100), nullable=False)
    user_email = db.Column(db.String(120), unique=True, nullable=False)
    user_pwd = db.Column(db.String(255), nullable=False)
    user_phone = db.Column(db.String(20))
    user_regdate = db.Column(db.DateTime)


class Admin(db.Model):
    __tablename__ = 'admin'

    adm_id = db.Column(db.Integer, primary_key=True)
    adm_username = db.Column(db.String(100), unique=True)
    adm_pwd = db.Column(db.String(255))
import os

# ---------------------------------------------------------------
# KayHomes – Instance Configuration
# This file is NOT committed to Git. Keep credentials here only.
# ---------------------------------------------------------------

DATABASE_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("MYSQL_URL")
)

if DATABASE_URL:
    if DATABASE_URL.startswith("mysql://"):
        DATABASE_URL = DATABASE_URL.replace(
            "mysql://",
            "mysql+pymysql://",
            1
        )
else:
    DATABASE_URL = "mysql+pymysql://root:@localhost/kayhomes"

SQLALCHEMY_DATABASE_URI = DATABASE_URL
SQLALCHEMY_TRACK_MODIFICATIONS = False
SECRET_KEY = os.getenv("SECRET_KEY", "securedkey")

# Mail settings for forgot-password emails.
# Update these values to your SMTP provider before sending real emails.
# MAIL_SERVER = 'smtp.gmail.com'
# MAIL_PORT = 587
# MAIL_USE_TLS = True
# MAIL_USE_SSL = False
# MAIL_USERNAME = 'your-email@example.com'
# MAIL_PASSWORD = 'your-app-password'
# MAIL_DEFAULT_SENDER = 'KayHomes <your-email@example.com>'

# Development default: don't attempt real SMTP delivery unless you configure values above.
MAIL_SUPPRESS_SEND = os.getenv("MAIL_SUPPRESS_SEND", "true").lower() == "true"

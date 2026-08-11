import os
import sys

from dotenv import load_dotenv


load_dotenv()

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

# Detect production mode
is_production = bool(
    os.getenv("RAILWAY_ENVIRONMENT")
    or os.getenv("RAILWAY_PROJECT_ID")
    or os.getenv("FLASK_ENV", "").lower() == "production"
)

# Resolve SECRET_KEY in order of preference
SECRET_KEY = None
secret_key_source = None

if os.getenv("SECRET_KEY"):
    SECRET_KEY = os.getenv("SECRET_KEY")
    secret_key_source = "SECRET_KEY"
elif os.getenv("FLASK_SECRET_KEY"):
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY")
    secret_key_source = "FLASK_SECRET_KEY"

if not SECRET_KEY:
    if is_production:
        print("[CONFIG] Production detected: True | Secret Key Found: False | Source: None", file=sys.stderr)
        raise RuntimeError("SECRET_KEY must be set in production.")
    else:
        SECRET_KEY = "dev-secret-key"
        secret_key_source = "fallback ('dev-secret-key')"

print(f"[CONFIG] Production detected: {is_production} | Secret Key Found: {bool(SECRET_KEY)} | Source: {secret_key_source}")

CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")

MAIL_SERVER = os.getenv("MAIL_SERVER", "127.0.0.1")
MAIL_PORT = int(os.getenv("MAIL_PORT", "25"))
MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "false").lower() == "true"
MAIL_USE_SSL = os.getenv("MAIL_USE_SSL", "false").lower() == "true"
MAIL_USERNAME = os.getenv("MAIL_USERNAME")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER", "noreply@kayhomes.local")
MAIL_SUPPRESS_SEND = os.getenv("MAIL_SUPPRESS_SEND", "true").lower() == "true"
import os

# ---------------------------------------------------------------
# KayHomes – Configuration
# ---------------------------------------------------------------

DATABASE_URL = os.getenv(
	"DATABASE_URL",
	"mysql+pymysql://root:@localhost/kayhomes"
)

SQLALCHEMY_DATABASE_URI = DATABASE_URL

SQLALCHEMY_TRACK_MODIFICATIONS = False

SECRET_KEY = os.getenv("SECRET_KEY", "securedkey")

MAIL_SERVER = os.getenv("MAIL_SERVER", "127.0.0.1")
MAIL_PORT = int(os.getenv("MAIL_PORT", "25"))
MAIL_USE_TLS = os.getenv("MAIL_USE_TLS", "false").lower() == "true"
MAIL_USE_SSL = os.getenv("MAIL_USE_SSL", "false").lower() == "true"
MAIL_USERNAME = os.getenv("MAIL_USERNAME")
MAIL_PASSWORD = os.getenv("MAIL_PASSWORD")
MAIL_DEFAULT_SENDER = os.getenv("MAIL_DEFAULT_SENDER", "noreply@kayhomes.local")
MAIL_SUPPRESS_SEND = os.getenv("MAIL_SUPPRESS_SEND", "true").lower() == "true"

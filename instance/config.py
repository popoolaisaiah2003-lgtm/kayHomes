# ---------------------------------------------------------------
# KayHomes – Instance Configuration
# This file is NOT committed to Git. Keep credentials here only.
# ---------------------------------------------------------------

# Original MySQL connection recovered from Git history (commit db82f69).
# User: root | Password: (none) | Host: localhost | DB: kayhomes
SQLALCHEMY_DATABASE_URI = 'mysql+pymysql://root:@localhost/kayhomes'

# Uncomment and update the line above if your MySQL password has changed, e.g.:
# SQLALCHEMY_DATABASE_URI = 'mysql+pymysql://root:YOUR_PASSWORD@localhost/kayhomes'

SQLALCHEMY_TRACK_MODIFICATIONS = False

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
MAIL_SUPPRESS_SEND = True

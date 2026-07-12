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

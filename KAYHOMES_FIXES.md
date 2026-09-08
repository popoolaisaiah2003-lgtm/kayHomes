# KayHomes Improvement Sprint

## Completed in this sprint

1. **App-like navigation**
   - Added Turbo Drive so normal internal navigation swaps the page without a full browser reload.
   - Re-initializes KayHomes JavaScript after Turbo navigation.
   - Stops background pollers before Turbo renders a new page to avoid duplicate polling and stale DOM references.

2. **Email validation and verification**
   - Registration now normalizes and validates email addresses with deliverability checks.
   - New accounts receive a cryptographically random, hashed verification token that expires after 24 hours.
   - Added `/verify-email/<token>` and `/resend-verification` flows.
   - New unverified accounts cannot log in until verified.
   - Existing accounts without a verification token remain usable for backward compatibility.

3. **SQL injection audit**
   - User-controlled values in raw SQL are passed through bound parameters (`:pid`, `:uid`, etc.).
   - Dynamic SQL fragments found in the codebase are schema-derived identifiers/DDL, not request values.
   - No request parameter was found directly interpolated into a SQL value clause during the audit.
   - Numeric route parameters use Flask's `<int:...>` converters.

4. **Local-time display**
   - Added client-side local timezone formatting for marked timestamps using the browser's `Intl.DateTimeFormat`.
   - Database timestamps remain stored as UTC/UTC-equivalent server timestamps rather than being duplicated per user timezone.
   - Chat API messages now expose an ISO timestamp for local conversion.

5. **Form persistence**
   - Added sessionStorage persistence for registration, login and property-posting forms.
   - Password and file fields are deliberately excluded.
   - State is restored after validation/server errors and cleared when navigating to a different path.

## Not included yet

- The portfolio/CV PDF was not changed because no CV/resume file exists in the uploaded KayHomes project archive. The PDF should be added from the actual CV source in the portfolio project.

## Verification performed

- Python syntax compilation passed for `pkg/*.py`.
- JavaScript syntax check passed for `pkg/static/app-live.js`.
- `.env`, local database files, and `.git` metadata were excluded from the handoff archive.

## Deployment notes

Production email verification requires valid SMTP environment variables. Set `MAIL_SUPPRESS_SEND=false` and configure `MAIL_SERVER`, `MAIL_PORT`, TLS/SSL, credentials, and `MAIL_DEFAULT_SENDER` in the deployment environment.

Run database migrations before deployment:

    flask db upgrade

The new migration is `20260908_email_verification`.

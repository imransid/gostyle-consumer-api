# Email

Email goes out through the **SendGrid Mail Send API** over HTTPS, using the
official `sendgrid` SDK, behind a Django email backend
(`apps/accounts/sendgrid_backend.py`). There is no SMTP path and no fallback to
one. Every `send_mail` / `EmailMultiAlternatives` call goes through that
backend unchanged.

This service sends two kinds of email, both only when `OTP_SENDER` is not
`console`:

- **Code emails**: the OTP for an email destination, and 2-step
  verification's sign-in and security codes. A refused send raises and the
  request fails, as it did under SMTP.
- **Notices**: something about the account just changed, and the welcome. A
  refused send is logged and swallowed, because the change has already been
  made.

Booking and reminder emails are sent by gostyle-booking-api, from the same
SendGrid account.

## Configuration

| Variable | Needed | Default | |
| --- | --- | --- | --- |
| `SENDGRID_API_KEY` | yes, to send | none | A key with Mail Send access. Unset or blank: sending raises `ImproperlyConfigured` naming the variable. It is read at startup, never logged, never returned. |
| `SENDGRID_FROM_EMAIL` | no | `noreply@gostyle.uk` | Must be on `gostyle.uk`, the domain authenticated in SendGrid. SendGrid refuses any other sender. |
| `SENDGRID_FROM_NAME` | no | `GoStyle` | |
| `OTP_SENDER` | no | `console` | `console` prints codes and notices and sends nothing. Any other value (compose sets `live`) uses WhatsApp for phones and SendGrid for emails. |

These are the names gostyle-platform and gostyle-booking-api read, so one
account and one authenticated domain serve all three. `DEFAULT_FROM_EMAIL` is
derived from the two sender variables (`GoStyle <noreply@gostyle.uk>`); it is
not set separately.

No longer read, and safe to remove from any deployment: `EMAIL_HOST`,
`EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`,
`DEFAULT_FROM_EMAIL`.

`EMAIL_BACKEND` can still be set to Django's console or in-memory backend to
see mail without sending it. Setting it to the SMTP backend is no longer
supported: its settings are gone.

## What is sent

### Code emails

One template per OTP purpose, as in gostyle-platform's `EmailOtpSender`. The
account holder's first name is used in the greeting when there is one.

| Purpose | Subject | Headline |
| --- | --- | --- |
| `register` | Verify your email address | `Hi <first name>,` or `Welcome,` |
| `password_reset` | Reset your password | `Hi <first name>,` or `Reset your password` |
| anything else (`login`, `change_contact`) | Your verification code | Confirm it’s you |
| 2-step sign-in code | Your Go Style sign-in code | Confirm it’s you |
| 2-step security code (turning 2-step on/off, the authenticator) | Your Go Style security code | Confirm it’s you |

Each is multipart: a plain-text part and the branded HTML, both drawn from the
same strings (`apps/accounts/emails.py`). The shell (black sheet, logo, rules,
footer) and the three bodies are a hand port of the platform's
`apps/gostyle-api/src/email/templates/`. **When those change, change this with
them**; `tests/test_emails.py` mirrors the platform's specs so a drift in the
words shows up as a failing test. Every value that reaches the HTML is escaped.

The code never appears in the inbox preview line, the subject or the logs.

### Notices

The same shell, with a serif headline over the notice's paragraphs (the body
style of gostyle-booking-api's booking emails). Sent only to an email the
account has verified, except the contact change notice, which goes to the
address being replaced.

| When | Subject | Sent from |
| --- | --- | --- |
| The account is first verified, by its email | Welcome to GoStyle | `services.verify_otp_for_user` |
| A sign-in from a device the account has not used | Go Style: new sign-in to your account | `sessions.start` |
| The email is changed (to the old address) | Your Go Style account details changed | `services.confirm_contact_change` |
| The phone number is changed (to the verified email) | Your Go Style account details changed | `services.confirm_contact_change` |
| The password is changed or reset | Go Style: your password was changed | `services.change_password`, `services.reset_password` |
| A deletion is scheduled | Go Style: your account is scheduled for deletion | `DeleteAccountView` |
| A scheduled deletion is cancelled | Go Style: your account will not be deleted | `CancelDeletionView` |
| The account is purged, 30 days on (to the address it had) | Go Style: your account has been deleted | `ConsumerAccount.purge` |
| 2-step turned on / off, authenticator added / removed | Go Style: 2-step verification turned on, ... | `two_factor.notify` |

Everything but the 2-step notices and the old-email notice is new with
SendGrid: before, those changes sent nothing. Some details:

- **Welcome**: once per account, the first time it becomes verified. An
  account verified by its phone has no verified email then, so it gets none,
  and verifying an email later does not send one either.
- **New sign-in**: needs `DEVICE_SESSIONS_V1` on, since the device is known
  only from its sessions. A device is known when an earlier session, live or
  signed out, had its device id, or with no id, its name and platform
  (`Chrome on macOS`). The registration's own session is not news, a refreshed
  token is not a sign-in, and a client that sends no device headers at all is
  news once rather than at every sign-in.
- **Account deleted**: the address is read before the purge clears it, and
  the email goes once the purge commits. A purge that fails sends nothing.

Booking and payment email (confirmed, rescheduled, cancelled, reminders,
receipts, refunds) belongs to gostyle-booking-api, behind its
`BOOKING_EMAIL_V1` and `REMINDER_DELIVERY` switches; this service does not
send any, so nothing goes out twice.

## Rules worth knowing

- **Accepted is not delivered.** A returned send means SendGrid accepted the
  message (HTTP 202). Whether it reached an inbox shows only in SendGrid's
  Activity feed, under the message id the service logs
  (`SendGrid accepted an email status=202 message_id=...`).
- **A refusal is loud.** A bad key (401), an unverified sender (403) or a
  rejected address (400) raises `SendGridError` carrying SendGrid's own reason,
  e.g. `SendGrid HTTP 403: The from address does not match a verified Sender
  Identity. (field: from)`. The address, subject and body are not in the log or
  the error. A notice logs the same error and carries on.
- **No retry here.** An OTP request that cannot send fails the request.
  Retrying is the caller's choice (the member asks again; the resend cooldown
  applies).
- **Ten-second timeout** on each call, so a hung connection cannot hold a
  request open.
- **Click tracking is off**, so links go out exactly as written.

## Sending yourself a test

There is deliberately **no endpoint** for this. From a shell on the server (or
locally with a real key in `.env`):

```sh
python manage.py send_test_email --to you@example.com --template password_reset
```

It sends the real template through the configured backend; a code in it is the
placeholder `000000`, which was never issued. `--template` is one of
`register` (default), `login`, `password_reset`, `change_contact`,
`two_factor_sign_in`, `two_factor_security`, `welcome`,
`notice_new_sign_in`, `notice_password_changed`, `notice_phone_changed`,
`notice_two_step_off`, `notice_deletion_scheduled`,
`notice_deletion_cancelled` or `notice_account_deleted`; `--first-name Sara`
shows the greeting on the code emails. It exits non-zero with SendGrid's reason if the send is
refused.

## Tests

Nothing in the suite reaches SendGrid. Backend tests stand in for the SDK client
(and one drives the real SDK against a fake HTTP opener, to check the URL, key,
body and timeout actually sent); the OTP and notice tests check the real
endpoints produce the right email. `talhajubair100.bd@gmail.com` is the
recipient used throughout.

```sh
python manage.py test apps.accounts.tests.test_sendgrid_backend \
  apps.accounts.tests.test_emails apps.accounts.tests.test_otp_email_delivery \
  apps.accounts.tests.test_notifications apps.accounts.tests.test_send_test_email \
  apps.accounts.tests.test_account_emails
```

"""The customer app's emails, ported from the platform's TypeScript templates.

Source of truth is gostyle-platform, apps/gostyle-api/src/email/templates:
email-layout.ts (the shell) and otp.template.ts, register-otp.template.ts and
password-reset.template.ts (the three code bodies). The notice body (no code:
a password changed, 2-step turned off) follows gostyle-booking-api's
booking-email.layout.ts, a serif headline over plain paragraphs. These are
hand ports, so they WILL drift -- if the brand changes in the platform, change
it here too, or this service's mail stops matching the rest of the product's.

Table layout and inline styles throughout: Outlook and most webmail strip
<style> blocks and ignore flex/grid. The one <style> that survives carries
only the mobile breakpoint.

Every value that reaches the HTML goes through esc(). A member's first name is
text someone typed; it does not get to become markup in an inbox.
"""

import math
from collections import namedtuple
from datetime import datetime

from .models import Purpose

BRAND = "GoStyle"

BRAND_LOGO_URL = (
    "https://entity-blob-storage.s3.eu-north-1.amazonaws.com/tenants/"
    "00000000-0000-0000-0000-000000000001/uploads/1788430458845-u9ffb37yqed.png"
)

INK = "#FFFFFF"
BODY_TEXT = "#C9C9C9"
MUTED_TEXT = "#8A8A8A"
RULE_COLOR = "#2A2A2A"
SHEET_BG = "#000000"


def esc(value):
    """Escape anything that reaches the HTML."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _style_block():
    return f"""
  <style type="text/css">
    body, table, td, a {{ -webkit-text-size-adjust: 100%; -ms-text-size-adjust: 100%; }}
    table, td {{ mso-table-lspace: 0pt; mso-table-rspace: 0pt; border-collapse: collapse; }}
    img {{ -ms-interpolation-mode: bicubic; border: 0; outline: none; text-decoration: none; }}
    body {{ margin: 0 !important; padding: 0 !important; width: 100% !important; }}
    a {{ color: {INK}; }}

    @media only screen and (max-width: 600px) {{
      .sheet    {{ width: 100% !important; }}
      .pad      {{ padding-left: 24px !important; padding-right: 24px !important; }}
      .headline {{ font-size: 24px !important; }}
      .code     {{ font-size: 28px !important; letter-spacing: 8px !important; text-indent: 8px !important; }}
    }}
  </style>"""


def _preheader(text):
    """The grey line next to the subject in the inbox. Leads with the brand,
    never the code, so a lock-screen preview does not leak it."""
    return f"""
  <div style="display:none; font-size:1px; line-height:1px; max-height:0; max-width:0; opacity:0; overflow:hidden; mso-hide:all;">
    {text}
  </div>
  <div style="display:none; font-size:1px; line-height:1px; max-height:0; max-width:0; opacity:0; overflow:hidden; mso-hide:all;">
    &#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;&#847;&zwnj;&nbsp;
  </div>"""


def _header():
    return f"""
          <tr>
            <td align="center" style="padding:36px 48px 24px 48px; background-color:{SHEET_BG}; border-radius:12px 12px 0 0;">
              <img src="{BRAND_LOGO_URL}" width="120" alt="GoStyle" style="display:block; border:0; max-width:120px; height:auto; margin:0 auto;" />
            </td>
          </tr>
          <tr>
            <td class="pad" style="padding:0 48px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr><td height="1" style="height:1px; line-height:1px; font-size:0; background-color:{RULE_COLOR};">&nbsp;</td></tr>
              </table>
            </td>
          </tr>"""


def _footer_divider():
    return f"""
          <tr>
            <td class="pad" style="padding:32px 48px 0 48px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
                <tr><td height="1" style="height:1px; line-height:1px; font-size:0; background-color:{RULE_COLOR};">&nbsp;</td></tr>
              </table>
            </td>
          </tr>"""


def _footer(app_name, year, extra_line=None):
    extra = f"{extra_line}<br />" if extra_line else ""
    return f"""
          <tr>
            <td class="pad" align="left" style="padding:20px 48px 40px 48px; font-family:Helvetica,Arial,sans-serif; font-size:12px; line-height:20px; mso-line-height-rule:exactly; color:{MUTED_TEXT}; border-radius:0 0 12px 12px;">
              {extra}Automated message from {app_name}. Replies to this address are not read.<br />
              &copy; {year} {app_name}. All rights reserved.
            </td>
          </tr>"""


def render_email_shell(title, preheader_text, body_rows, app_name,
                       width=560, footer_extra_line=None, year=None):
    """The black sheet, logo header, rules and footer around `body_rows`.

    `title`, `preheader_text` and `app_name` go in as given: the caller escapes
    them. `year` defaults to now; a test passes one so it does not age with the
    calendar.
    """
    year = year if year is not None else datetime.now().year
    return f"""<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0 Transitional//EN" "http://www.w3.org/TR/xhtml1/DTD/xhtml1-transitional.dtd">
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:o="urn:schemas-microsoft-com:office:office" lang="en">
<head>
  <meta http-equiv="Content-Type" content="text/html; charset=UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <meta name="x-apple-disable-message-reformatting" />
  <title>{title}</title>
  <!--[if mso]>
  <xml><o:OfficeDocumentSettings><o:PixelsPerInch>96</o:PixelsPerInch></o:OfficeDocumentSettings></xml>
  <![endif]-->{_style_block()}
</head>
<body style="margin:0; padding:0;">{_preheader(preheader_text)}
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0">
    <tr>
      <td align="center" style="padding:40px 12px;">
        <table role="presentation" width="{width}" cellpadding="0" cellspacing="0" border="0" class="sheet" style="width:{width}px; max-width:{width}px; background-color:{SHEET_BG}; border-radius:12px;">{_header()}{body_rows}{_footer_divider()}{_footer(app_name, year, footer_extra_line)}
        </table>
      </td>
    </tr>
  </table>
</body>
</html>"""


def _minutes(n):
    return f"{n} minute" if n == 1 else f"{n} minutes"


def _render_code_email(*, title, preheader_text, headline, intro, code,
                       expires_in_minutes, footnote, app_name, year):
    """The body all three code emails share: a serif headline, one line of
    introduction, the code on a white card with its expiry, and a footnote.
    Only the words differ between them, so only the words are parameters."""
    body_rows = f"""
          <tr>
            <td class="pad headline" align="left" style="padding:36px 48px 0 48px; font-family:Georgia,'Times New Roman',serif; font-size:28px; line-height:34px; mso-line-height-rule:exactly; color:{INK};">
              {esc(headline)}
            </td>
          </tr>
          <tr>
            <td class="pad" align="left" style="padding:14px 48px 0 48px; font-family:Helvetica,Arial,sans-serif; font-size:15px; line-height:24px; mso-line-height-rule:exactly; color:{BODY_TEXT};">
              {esc(intro)}
            </td>
          </tr>
          <tr>
            <td class="pad" style="padding:28px 48px 0 48px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#FFFFFF;">
                <tr>
                  <td align="center" class="code" style="padding:34px 16px 0 16px; font-family:Helvetica,Arial,sans-serif; font-size:40px; line-height:46px; mso-line-height-rule:exactly; font-weight:bold; color:#000000; letter-spacing:12px; text-indent:12px;">{esc(code)}</td>
                </tr>
                <tr>
                  <td align="center" style="padding:12px 16px 30px 16px; font-family:Helvetica,Arial,sans-serif; font-size:12px; line-height:18px; mso-line-height-rule:exactly; color:#6B6B6B;">
                    Expires in {_minutes(expires_in_minutes)}
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td class="pad" align="left" style="padding:28px 48px 0 48px; font-family:Helvetica,Arial,sans-serif; font-size:14px; line-height:23px; mso-line-height-rule:exactly; color:{MUTED_TEXT};">
              {esc(footnote)}
            </td>
          </tr>"""

    return render_email_shell(
        title=esc(title),
        preheader_text=esc(preheader_text),
        body_rows=body_rows,
        app_name=esc(app_name),
        year=year,
    )


def _never_ask(app_name):
    return f"{app_name} will never ask you for this code on a call, on WhatsApp, or in chat."


def _copy_otp(minutes, first_name, app_name):
    return {
        "subject": "Your verification code",
        "title": "Your Verification Code",
        "preheader_text": f"Your {app_name} sign in code is ready. It expires in {_minutes(minutes)}.",
        "headline": "Confirm it’s you",
        "intro": f"Enter this code to verify your {app_name} account.",
        "footnote": f"{_never_ask(app_name)} If you did not request this code, you can safely ignore this email.",
    }


def _copy_register(minutes, first_name, app_name):
    return {
        "subject": "Verify your email address",
        "title": "Verify Your Email",
        "preheader_text": f"Your {app_name} verification code is ready. It expires in {_minutes(minutes)}.",
        "headline": f"Hi {first_name}," if first_name else "Welcome,",
        "intro": (
            f"Thanks for signing up. Enter this code in the {app_name} app to verify "
            "your email and finish creating your account."
        ),
        "footnote": (
            f"{_never_ask(app_name)} Didn't create this account? You can safely "
            "ignore this email — no action is needed."
        ),
    }


def _copy_password_reset(minutes, first_name, app_name):
    return {
        "subject": "Reset your password",
        "title": "Reset Your Password",
        "preheader_text": f"Your {app_name} password reset code is ready. It expires in {_minutes(minutes)}.",
        "headline": f"Hi {first_name}," if first_name else "Reset your password",
        "intro": (
            f"We received a request to reset the password on your {app_name} account. "
            "Enter this code in the app to choose a new one."
        ),
        "footnote": (
            f"{_never_ask(app_name)} Didn't ask to reset your password? You can "
            "safely ignore this email — your password stays the same."
        ),
    }


# Which words go with which OTP purpose, as in the platform's EmailOtpSender:
# registration and password reset have their own; everything else is the
# generic "confirm it's you".
_COPY_BY_PURPOSE = {
    Purpose.REGISTER: _copy_register,
    Purpose.PASSWORD_RESET: _copy_password_reset,
}

Email = namedtuple("Email", ["subject", "text", "html"])

_AUTOMATED = "Automated message from {app_name}. Replies to this address are not read."


def build_otp_email(purpose, code, expires_in_minutes, first_name=None,
                    subject=None, app_name=BRAND, year=None):
    """The subject, plain text and HTML of the code email for `purpose`.

    `subject` replaces the template's own, for a code whose words are the
    generic ones but whose inbox line says what it is for (2-step
    verification's sign-in and security codes).

    The text and the HTML are drawn from the same strings, so a client that
    refuses HTML is told exactly what the HTML says.
    """
    copy = _COPY_BY_PURPOSE.get(purpose, _copy_otp)(expires_in_minutes, first_name, app_name)

    html = _render_code_email(
        title=copy["title"],
        preheader_text=copy["preheader_text"],
        headline=copy["headline"],
        intro=copy["intro"],
        code=code,
        expires_in_minutes=expires_in_minutes,
        footnote=copy["footnote"],
        app_name=app_name,
        year=year,
    )
    text = "\n".join(
        [
            copy["headline"],
            "",
            copy["intro"],
            "",
            f"Your code: {code}",
            f"It expires in {_minutes(expires_in_minutes)}.",
            "",
            copy["footnote"],
            "",
            _AUTOMATED.format(app_name=app_name),
        ]
    )
    return Email(subject or copy["subject"], text, html)


def build_notice_email(subject, headline, message, preheader=None, app_name=BRAND,
                       year=None):
    """A notice with no code in it: something about the account just changed
    (a password, a contact, 2-step verification, a deletion scheduled, a new
    sign-in), or the welcome once it is verified.

    `message` is the plain text the notice has always been, paragraphs split
    by a blank line. The text part is that message as written; the HTML draws
    each paragraph under `headline`, the last one muted, as the code emails
    draw their footnote. `preheader` is the inbox preview line, by default
    the generic security one.
    """
    paragraphs = [p.strip() for p in message.split("\n\n") if p.strip()]
    rows = [
        f"""
          <tr>
            <td class="pad headline" align="left" style="padding:36px 48px 0 48px; font-family:Georgia,'Times New Roman',serif; font-size:28px; line-height:34px; mso-line-height-rule:exactly; color:{INK};">
              {esc(headline)}
            </td>
          </tr>"""
    ]
    for i, paragraph in enumerate(paragraphs):
        last = len(paragraphs) > 1 and i == len(paragraphs) - 1
        rows.append(
            f"""
          <tr>
            <td class="pad" align="left" style="padding:{28 if last else 14}px 48px 0 48px; font-family:Helvetica,Arial,sans-serif; font-size:{14 if last else 15}px; line-height:{23 if last else 24}px; mso-line-height-rule:exactly; color:{MUTED_TEXT if last else BODY_TEXT};">
              {esc(paragraph)}
            </td>
          </tr>"""
        )

    html = render_email_shell(
        title=esc(subject),
        preheader_text=esc(preheader or f"A security notice about your {app_name} account."),
        body_rows="".join(rows),
        app_name=esc(app_name),
        year=year,
    )
    text = "\n\n".join([message.strip(), _AUTOMATED.format(app_name=app_name)])
    return Email(subject, text, html)


def minutes_from_seconds(seconds):
    """A TTL in seconds as the whole minutes an email states, rounded up as the
    platform's sender does. The OTP TTL is a whole number of minutes, so the
    rounding only matters if someone changes it to one that is not."""
    return max(1, math.ceil(seconds / 60))

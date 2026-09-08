"""HTML for the OTP email, ported from the platform's TypeScript templates.

Source of truth is gostyle-platform: email-layout.ts (the shell) and
otp-email.ts (the body). This is a hand port, so the two WILL drift — if the
brand changes there, change it here too, or the codes stop matching the rest
of the product's mail.

Table layout and inline styles throughout: Outlook and most webmail strip
<style> blocks and ignore flex/grid. The one <style> that survives carries
only the mobile breakpoint.
"""

from datetime import datetime

BRAND_LOGO_URL = (
    "https://entity-blob-storage.s3.eu-north-1.amazonaws.com/tenants/"
    "00000000-0000-0000-0000-000000000001/uploads/1788430458845-u9ffb37yqed.png"
)

INK = "#FFFFFF"
BODY_TEXT = "#C9C9C9"
MUTED_TEXT = "#8A8A8A"
RULE_COLOR = "#2A2A2A"
SHEET_BG = "#000000"


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
                       width=560, footer_extra_line=None):
    year = datetime.now().year
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


def render_otp_email(code, expires_in_minutes, app_name="GoStyle"):
    body_rows = f"""
          <tr>
            <td class="pad headline" align="left" style="padding:36px 48px 0 48px; font-family:Georgia,'Times New Roman',serif; font-size:28px; line-height:34px; mso-line-height-rule:exactly; color:{INK};">
              Confirm it&rsquo;s you
            </td>
          </tr>
          <tr>
            <td class="pad" align="left" style="padding:14px 48px 0 48px; font-family:Helvetica,Arial,sans-serif; font-size:15px; line-height:24px; mso-line-height-rule:exactly; color:{BODY_TEXT};">
              Enter this code to verify your {app_name} account.
            </td>
          </tr>
          <tr>
            <td class="pad" style="padding:28px 48px 0 48px;">
              <table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" style="background-color:#FFFFFF;">
                <tr>
                  <td align="center" class="code" style="padding:34px 16px 0 16px; font-family:Helvetica,Arial,sans-serif; font-size:40px; line-height:46px; mso-line-height-rule:exactly; font-weight:bold; color:#000000; letter-spacing:12px; text-indent:12px;">{code}</td>
                </tr>
                <tr>
                  <td align="center" style="padding:12px 16px 30px 16px; font-family:Helvetica,Arial,sans-serif; font-size:12px; line-height:18px; mso-line-height-rule:exactly; color:#6B6B6B;">
                    Expires in {expires_in_minutes} minutes
                  </td>
                </tr>
              </table>
            </td>
          </tr>
          <tr>
            <td class="pad" align="left" style="padding:28px 48px 0 48px; font-family:Helvetica,Arial,sans-serif; font-size:14px; line-height:23px; mso-line-height-rule:exactly; color:{MUTED_TEXT};">
              {app_name} will never ask you for this code on a call, on WhatsApp, or in chat. If you did not request this code, you can safely ignore this email.
            </td>
          </tr>"""

    return render_email_shell(
        title="Your Verification Code",
        preheader_text=f"Your {app_name} sign in code is ready. It expires in {expires_in_minutes} minutes.",
        body_rows=body_rows,
        app_name=app_name,
    )
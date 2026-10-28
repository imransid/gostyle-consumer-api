"""
Tests for the email templates (apps/accounts/emails.py).

The code emails mirror the platform's template specs (otp / register-otp /
password-reset .template.spec.ts), so a change to the words there that is
copied here, or not, shows up as a failing test rather than as a customer
noticing. The notices are this service's own.
"""

from django.test import SimpleTestCase

from apps.accounts.emails import (
    BRAND_LOGO_URL,
    build_notice_email,
    build_otp_email,
    esc,
    minutes_from_seconds,
    render_email_shell,
)
from apps.accounts.models import Purpose


class OtpEmailTests(SimpleTestCase):
    def build(self, **kwargs):
        args = {"purpose": Purpose.LOGIN, "code": "482913", "expires_in_minutes": 5}
        args.update(kwargs)
        return build_otp_email(**args)

    def test_renders_the_code_and_expiry_window(self):
        mail = self.build()

        self.assertIn("482913", mail.html)
        self.assertIn("5 minutes", mail.html)

    def test_defaults_the_app_name_to_gostyle(self):
        self.assertIn("GoStyle", self.build().html)

    def test_honours_a_custom_app_name(self):
        self.assertIn("Acme", self.build(app_name="Acme").html)

    def test_the_generic_one_is_confirm_its_you(self):
        mail = self.build()

        self.assertEqual(mail.subject, "Your verification code")
        self.assertIn("Confirm it’s you", mail.html)
        self.assertIn("<title>Your Verification Code</title>", mail.html)

    def test_an_unknown_purpose_gets_the_generic_one(self):
        self.assertEqual(self.build(purpose=None).subject, "Your verification code")
        self.assertEqual(self.build(purpose="something_new").subject, "Your verification code")

    def test_a_singular_minute_is_not_pluralised(self):
        html = self.build(expires_in_minutes=1).html

        self.assertRegex(html, r"Expires in 1 minute\s*</td>")
        self.assertNotIn("1 minutes", html)


class RegisterEmailTests(SimpleTestCase):
    def build(self, **kwargs):
        args = {"purpose": Purpose.REGISTER, "code": "748210", "expires_in_minutes": 5}
        args.update(kwargs)
        return build_otp_email(**args)

    def test_renders_the_code_and_expiry_window(self):
        mail = self.build()

        self.assertIn("748210", mail.html)
        self.assertIn("5 minutes", mail.html)

    def test_subject_and_title_are_the_registration_ones(self):
        mail = self.build()

        self.assertEqual(mail.subject, "Verify your email address")
        self.assertIn("<title>Verify Your Email</title>", mail.html)

    def test_greets_by_first_name_when_provided(self):
        self.assertIn("Hi Rafa,", self.build(first_name="Rafa").html)

    def test_falls_back_to_a_welcome_greeting_without_a_first_name(self):
        self.assertIn("Welcome,", self.build().html)

    def test_says_it_is_for_finishing_an_account(self):
        self.assertIn("finish creating your account", self.build().html)


class PasswordResetEmailTests(SimpleTestCase):
    def build(self, **kwargs):
        args = {"purpose": Purpose.PASSWORD_RESET, "code": "482913", "expires_in_minutes": 5}
        args.update(kwargs)
        return build_otp_email(**args)

    def test_renders_the_code_and_expiry_window(self):
        mail = self.build(expires_in_minutes=10)

        self.assertIn("482913", mail.html)
        self.assertIn("10 minutes", mail.html)

    def test_says_it_is_a_password_reset_not_a_sign_up(self):
        html = self.build().html

        self.assertIn("reset the password", html)
        self.assertNotIn("signing up", html)

    def test_subject_and_title_are_the_reset_ones(self):
        mail = self.build()

        self.assertEqual(mail.subject, "Reset your password")
        self.assertIn("<title>Reset Your Password</title>", mail.html)

    def test_greets_by_first_name_when_known(self):
        self.assertIn("Hi Sara,", self.build(first_name="Sara").html)

    def test_falls_back_to_a_headline_without_a_name(self):
        html = self.build().html

        self.assertIn("Reset your password", html)
        self.assertNotIn("Hi ", html)


class SafetyTests(SimpleTestCase):
    def test_a_first_name_cannot_become_markup(self):
        mail = build_otp_email(
            Purpose.REGISTER, "123456", 5, first_name='<script>alert("x")</script>&Co'
        )

        self.assertNotIn("<script>", mail.html)
        self.assertIn("&lt;script&gt;", mail.html)
        self.assertIn("&amp;Co", mail.html)

    def test_esc_covers_the_four_characters(self):
        self.assertEqual(esc('<a href="x">&</a>'), "&lt;a href=&quot;x&quot;&gt;&amp;&lt;/a&gt;")

    def test_the_preheader_leads_with_the_brand_and_never_the_code(self):
        mail = build_otp_email(Purpose.LOGIN, "482913", 5)
        preheader = mail.html.split("display:none", 1)[1].split("</div>", 1)[0]

        self.assertIn("GoStyle", preheader)
        self.assertNotIn("482913", preheader)


class PlainTextTests(SimpleTestCase):
    """The text part is for clients that refuse HTML. It must carry the code
    and say the same thing the HTML does."""

    def test_the_text_has_the_code_the_expiry_and_the_warning(self):
        mail = build_otp_email(Purpose.LOGIN, "482913", 5)

        self.assertIn("Your code: 482913", mail.text)
        self.assertIn("expires in 5 minutes", mail.text)
        self.assertIn("will never ask you for this code", mail.text)
        self.assertNotIn("<", mail.text)

    def test_the_text_greets_the_same_way_the_html_does(self):
        named = build_otp_email(Purpose.REGISTER, "1", 5, first_name="Rafa")
        anonymous = build_otp_email(Purpose.REGISTER, "1", 5)

        self.assertTrue(named.text.startswith("Hi Rafa,"))
        self.assertTrue(anonymous.text.startswith("Welcome,"))

    def test_the_text_is_not_html_escaped(self):
        mail = build_otp_email(Purpose.REGISTER, "1", 5, first_name="O'Brien & Co")

        self.assertIn("Hi O'Brien & Co,", mail.text)


class ShellTests(SimpleTestCase):
    def test_the_shell_has_the_logo_footer_and_year(self):
        html = render_email_shell("T", "pre", "", "GoStyle", year=2031)

        self.assertIn(BRAND_LOGO_URL, html)
        self.assertIn("&copy; 2031 GoStyle. All rights reserved.", html)
        self.assertIn("Replies to this address are not read.", html)

    def test_the_year_defaults_to_now(self):
        from datetime import datetime

        self.assertIn(f"&copy; {datetime.now().year} GoStyle", render_email_shell("T", "p", "", "GoStyle"))

    def test_a_footer_extra_line_sits_above_the_notice(self):
        html = render_email_shell("T", "p", "", "GoStyle", footer_extra_line="Sent on behalf of X")

        self.assertLess(html.index("Sent on behalf of X"), html.index("Automated message"))


class TtlTests(SimpleTestCase):
    def test_seconds_become_whole_minutes(self):
        self.assertEqual(minutes_from_seconds(300), 5)
        self.assertEqual(minutes_from_seconds(600), 10)

    def test_a_part_minute_rounds_up_and_never_to_zero(self):
        self.assertEqual(minutes_from_seconds(90), 2)
        self.assertEqual(minutes_from_seconds(10), 1)


class SubjectOverrideTests(SimpleTestCase):
    """2-step verification's codes: the generic words under a subject of
    their own."""

    def test_a_subject_replaces_the_templates_own_and_nothing_else(self):
        plain = build_otp_email(None, "482913", 10)
        named = build_otp_email(None, "482913", 10, subject="Your Go Style sign-in code")

        self.assertEqual(named.subject, "Your Go Style sign-in code")
        self.assertEqual((named.text, named.html), (plain.text, plain.html))


class NoticeEmailTests(SimpleTestCase):
    MESSAGE = (
        "2-step verification was turned off for your Go Style account on iPhone 15.\n\n"
        "If this was not you, reset your password and contact support straight away."
    )

    def build(self, **kwargs):
        args = {
            "subject": "Go Style: 2-step verification turned off",
            "headline": "2-step verification turned off",
            "message": self.MESSAGE,
            "year": 2031,
        }
        args.update(kwargs)
        return build_notice_email(**args)

    def test_the_text_is_the_message_as_written_then_the_footer(self):
        mail = self.build()

        self.assertEqual(
            mail.text,
            f"{self.MESSAGE}\n\nAutomated message from GoStyle. Replies to this address are not read.",
        )

    def test_the_html_has_the_headline_and_every_paragraph_in_the_shell(self):
        mail = self.build()

        self.assertEqual(mail.subject, "Go Style: 2-step verification turned off")
        self.assertIn("<title>Go Style: 2-step verification turned off</title>", mail.html)
        self.assertIn("2-step verification turned off", mail.html)
        self.assertIn("for your Go Style account on iPhone 15.", mail.html)
        self.assertIn("If this was not you", mail.html)
        self.assertIn(BRAND_LOGO_URL, mail.html)
        self.assertIn("&copy; 2031 GoStyle", mail.html)

    def test_the_last_paragraph_is_the_muted_footnote(self):
        html = self.build().html
        footnote_row = html[: html.index("If this was not you")].rsplit("<td", 1)[1]

        self.assertIn("color:#8A8A8A", footnote_row)

    def test_a_single_paragraph_is_body_text_not_a_footnote(self):
        html = self.build(message="It was removed.").html
        row = html[: html.index("It was removed.")].rsplit("<td", 1)[1]

        self.assertIn("color:#C9C9C9", row)

    def test_nothing_in_a_notice_can_become_markup(self):
        mail = self.build(
            subject="<i>S</i>", headline="<b>H</b>", message="on <script>x</script>&Co"
        )

        for raw in ("<i>S</i>", "<b>H</b>", "<script>"):
            self.assertNotIn(raw, mail.html)
        self.assertIn("&lt;script&gt;x&lt;/script&gt;&amp;Co", mail.html)

    def test_the_preheader_is_generic(self):
        preheader = self.build().html.split("display:none", 1)[1].split("</div>", 1)[0]

        self.assertIn("A security notice about your GoStyle account.", preheader)

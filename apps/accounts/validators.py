import re

from django.core.exceptions import ValidationError
from django.utils.translation import gettext as _

# These mirror the strength rules shown on the "Create New Password" screen
# (8+ characters, numbers, uppercase letter, symbol). Django ships the length
# check (MinimumLengthValidator); the three below cover the rest so that
# register / reset serializers can share one policy via validate_password().


class UppercaseValidator:
    def validate(self, password, user=None):
        if not re.search(r"[A-Z]", password):
            raise ValidationError(
                _("Password must contain at least one uppercase letter."),
                code="password_no_upper",
            )

    def get_help_text(self):
        return _("Your password must contain at least one uppercase letter.")


class NumberValidator:
    def validate(self, password, user=None):
        if not re.search(r"\d", password):
            raise ValidationError(
                _("Password must contain at least one number."),
                code="password_no_number",
            )

    def get_help_text(self):
        return _("Your password must contain at least one number.")


class SymbolValidator:
    def validate(self, password, user=None):
        if not re.search(r"[^\w\s]", password):
            raise ValidationError(
                _("Password must contain at least one symbol (e.g. !@#)."),
                code="password_no_symbol",
            )

    def get_help_text(self):
        return _("Your password must contain at least one symbol, e.g. !@#.")

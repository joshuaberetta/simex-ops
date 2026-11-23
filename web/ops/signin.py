"""Sign-in links for the QR codes on the login slips. A link is a signed token for one
participant's, team leader's or persona desk member's login: scanning it signs them in on that device (any number of
devices) until their password is reset, their login is disabled or the link is TOKEN_DAYS old.
Nothing is stored; an ExCon login never gets one."""

from datetime import timedelta

import segno
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core import signing
from django.utils import timezone
from django.utils.crypto import constant_time_compare, salted_hmac

from .access import desk_of, is_excon, leader_of, participant_of

SALT = "ops.signin"
TOKEN_DAYS = 14  # as long as a session


def can_use(user):
    """A QR sign-in only for an active member's, team leader's or persona desk member's login,
    never ExCon's."""
    return bool(user and user.is_active and not is_excon(user)
                and (participant_of(user) or leader_of(user) or desk_of(user)))


def _stamp(user):
    # Changes when the password is reset (or the login disabled): every slip printed before stops working.
    return salted_hmac(SALT, f"{user.pk}:{user.password}:{user.is_active}").hexdigest()[:16]


def token(user):
    return signing.dumps([user.pk, _stamp(user)], salt=SALT)


def user_for(tok):
    """The login a token signs in, or None (bad, expired, reset, disabled, ExCon)."""
    try:
        pk, stamp = signing.loads(tok, salt=SALT, max_age=timedelta(days=TOKEN_DAYS))
    except (signing.BadSignature, ValueError, TypeError):
        return None
    user = get_user_model().objects.filter(pk=pk).first()
    if not user or not constant_time_compare(str(stamp), _stamp(user)) or not can_use(user):
        return None
    return user


def setup_path(user):
    """Where a QR sign-in lands: the device setup of the login's mailbox."""
    return "/inbox/desk/setup/" if desk_of(user) else "/inbox/setup/"


def path(user):
    return f"/inbox/go/{token(user)}/"


def url(user):
    return (settings.MAIL_URL or settings.PUBLIC_URL).rstrip("/") + path(user)


def expires():
    return timezone.localtime() + timedelta(days=TOKEN_DAYS)


def qr_svg(text):
    """The QR code as inline SVG, for printing."""
    return segno.make(text, error="m").svg_inline(border=4, omitsize=True)  # viewBox: sized by the page

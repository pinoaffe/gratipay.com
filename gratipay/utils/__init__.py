# encoding: utf8
from __future__ import absolute_import, division, print_function, unicode_literals

import fnmatch
import random
import os
import re
from base64 import urlsafe_b64encode, urlsafe_b64decode
from datetime import datetime, timedelta

from aspen import Response, json
from aspen.utils import to_rfc822, utcnow
from postgres.cursors import SimpleCursorBase

import gratipay


BEGINNING_OF_EPOCH = to_rfc822(datetime(1970, 1, 1)).encode('ascii')

# Difference between current time and credit card expiring date when
# card is considered as expiring
EXPIRING_DELTA = timedelta(days = 30)

_email_re = re.compile(r'^[^@]+@[^@]+\.[^@]+$')
# exactly one @, and at least one . after @ -- simple validation, send to be sure
def is_valid_email_address(email_address):
    return len(email_address) < 255 and _email_re.match(email_address)


def dict_to_querystring(mapping):
    if not mapping:
        return u''

    arguments = []
    for key, values in mapping.iteritems():
        for val in values:
            arguments.append(u'='.join([key, val]))

    return u'?' + u'&'.join(arguments)


def _munge(website, request, url_prefix, fs_prefix):
    """Given website and requests objects along with URL and filesystem
    prefixes, redirect or modify the request. The idea here is that sometimes
    for various reasons the dispatcher can't handle a mapping, so this is a
    hack to rewrite the URL to help the dispatcher map to the filesystem.

    If you access the filesystem version directly through the web, we redirect
    you to the URL version. If you access the URL version as desired, then we
    rewrite so we can find it on the filesystem.
    """
    if request.path.raw.startswith(fs_prefix):
        to = url_prefix + request.path.raw[len(fs_prefix):]
        if request.qs.raw:
            to += '?' + request.qs.raw
        website.redirect(to)
    elif request.path.raw.startswith(url_prefix):
        request.path.__init__(fs_prefix + request.path.raw[len(url_prefix):])

def use_tildes_for_participants(website, request):
    return _munge(website, request, '/~', '/~/')


def canonicalize(redirect, path, base, canonical, given, arguments=None):
    if given != canonical:
        assert canonical.lower() == given.lower()  # sanity check
        remainder = path[len(base + given):]

        if arguments is not None:
            arguments = dict_to_querystring(arguments)

        newpath = base + canonical + remainder + arguments or ''
        redirect(newpath)


def get_participant(state, restrict=True, resolve_unclaimed=True):
    """Given a Request, raise Response or return Participant.

    If restrict is True then we'll restrict access to owners and admins.

    """
    redirect = state['website'].redirect
    request = state['request']
    user = state['user']
    slug = request.line.uri.path['username']
    qs = request.line.uri.querystring
    _ = state['_']

    if restrict:
        if user.ANON:
            raise Response(401, _("You need to log in to access this page."))

    from gratipay.models.participant import Participant  # avoid circular import
    participant = Participant.from_username(slug)

    if participant is None:
        raise Response(404)

    canonicalize(redirect, request.line.uri.path.raw, '/~/', participant.username, slug, qs)

    if participant.is_closed:
        if user.ADMIN:
            return participant
        raise Response(410)

    if participant.claimed_time is None and resolve_unclaimed:
        to = participant.resolve_unclaimed()
        if to:
            # This is a stub account (someone on another platform who hasn't
            # actually registered with Gratipay yet)
            redirect(to)
        else:
            # This is an archived account (result of take_over)
            if user.ADMIN:
                return participant
            raise Response(404)

    if restrict:
        if participant != user.participant:
            if not user.ADMIN:
                raise Response(403, _("You are not authorized to access this page."))

    return participant


def encode_for_querystring(s):
    """Given a unicode, return a unicode that's safe for transport across a querystring.
    """
    if not isinstance(s, unicode):
        raise TypeError('unicode required')
    return urlsafe_b64encode(s.encode('utf8')).replace(b'=', b'~').decode('ascii')


def decode_from_querystring(s, **kw):
    """Given a unicode computed by encode_for_querystring, return the inverse.

    We raise Response(400) if the input value can't be decoded (i.e., it's not
    ASCII, not padded properly, or not decodable as UTF-8 once Base64-decoded).

    """
    if not isinstance(s, unicode):
        raise TypeError('unicode required')
    try:
        return urlsafe_b64decode(s.encode('ascii').replace(b'~', b'=')).decode('utf8')
    except:
        if 'default' in kw:
            # Enable callers to handle errors without using try/except.
            return kw['default']
        raise Response(400, "invalid input")


def _execute(this, sql, params=[]):
    print(sql.strip(), params)
    super(SimpleCursorBase, this).execute(sql, params)

def log_cursor(f):
    "Prints sql and params to stdout. Works globaly so watch for threaded use."
    def wrapper(*a, **kw):
        try:
            SimpleCursorBase.execute = _execute
            ret = f(*a, **kw)
        finally:
            del SimpleCursorBase.execute
        return ret
    return wrapper


def format_money(money):
    format = '%.2f' if money < 1000 else '%.0f'
    return format % money


def truncate(text, target=160, append=' …'):
    nchars = len(text)
    if nchars <= target:                                    # short enough already
        return text
    if append:                                              # recursive case
        return truncate(text, max(target-len(append), 0), '') + append
    truncated = text[:target]
    if not target or ' ' in (truncated[-1], text[target]):  # clean break
        return truncated.rstrip()
    return truncated.rsplit(' ', 1)[0]                      # trailing partial word


def is_card_expiring(expiration_year, expiration_month):
    now = datetime.utcnow()
    expiring_date = datetime(expiration_year, expiration_month, 1)
    delta = expiring_date - now
    return delta < EXPIRING_DELTA


def set_cookie(cookies, key, value, expires=None, httponly=True, path=b'/'):
    cookies[key] = value
    cookie = cookies[key]
    if expires:
        if isinstance(expires, timedelta):
            expires += utcnow()
        if isinstance(expires, datetime):
            expires = to_rfc822(expires).encode('ascii')
        cookie[b'expires'] = expires
    if httponly:
        cookie[b'httponly'] = True
    if path:
        cookie[b'path'] = path
    if gratipay.use_secure_cookies:
        cookie[b'secure'] = True


def erase_cookie(cookies, key, **kw):
    set_cookie(cookies, key, '', BEGINNING_OF_EPOCH, **kw)


def filter_profile_nav(user, participant, pages):
    out = []
    for foo, bar, show_them, show_others in pages:
        if (user.participant == participant and show_them) \
        or (user.participant != participant and show_others) \
        or user.ADMIN:
            out.append((foo, bar, show_them, show_others))
    return out


def to_javascript(obj):
    """For when you want to inject an object into a <script> tag.
    """
    return json.dumps(obj).replace('</', '<\\/')


def get_featured_projects(db):
    npopular, nunpopular = db.one("""

        WITH eligible_teams AS (
            SELECT *
              FROM teams
             WHERE not is_closed
               AND is_approved
        )

        SELECT (SELECT COUNT(1)
                  FROM eligible_teams
                 WHERE nreceiving_from > 5) AS npopular,

               (SELECT COUNT(1)
                  FROM eligible_teams
                 WHERE nreceiving_from <= 5) AS nunpopular

    """, back_as=tuple)

    # Attempt to maintain a 70-30 ratio
    if npopular >= 7:
        npopular = max(7, 10-nunpopular)

    # Fill in the rest with unpopular
    nunpopular = min(nunpopular, 10-npopular)

    featured_projects = db.all("""

        WITH eligible_teams AS (
            SELECT *
              FROM teams
             WHERE not is_closed
               AND is_approved
        )

        (SELECT t.*::teams
          FROM eligible_teams t
         WHERE nreceiving_from > 5
      ORDER BY random()
         LIMIT %(npopular)s)

         UNION

        (SELECT t.*::teams
          FROM eligible_teams t
         WHERE nreceiving_from <= 5
      ORDER BY random()
         LIMIT %(nunpopular)s)

    """, locals())

    random.shuffle(featured_projects)
    return featured_projects

def set_version_header(response, website):
    response.headers['X-Gratipay-Version'] = website.version


def find_files(directory, pattern):
    for root, dirs, files in os.walk(directory):
        for filename in fnmatch.filter(files, pattern):
            yield os.path.join(root, filename)

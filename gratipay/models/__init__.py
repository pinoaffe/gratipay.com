"""

The most important object in the Gratipay object model is Participant, and the
second most important one is Ccommunity. There are a few others, but those are
the most important two. Participant, in particular, is at the center of
everything on Gratipay.

"""
from contextlib import contextmanager

from postgres import Postgres

from .account_elsewhere import AccountElsewhere
from .community import Community
from .country import Country
from .exchange_route import ExchangeRoute
from .package import Package
from .participant import Participant
from .payment_for_open_source import PaymentForOpenSource
from .team import Team


MODELS = (AccountElsewhere, Community, Country, ExchangeRoute, Package, Participant,
          PaymentForOpenSource, Team)


@contextmanager
def just_yield(obj):
    yield obj


class GratipayDB(Postgres):
    """Model the Gratipay database.
    """

    def __init__(self, app, *a, **kw):
        """Extend to make the ``Application`` object available on models at
        ``.app``.
        """
        Postgres.__init__(self, *a, **kw)
        for model in MODELS:
            self.register_model(model)
            model.app = app

    def get_cursor(self, cursor=None, **kw):
        if cursor:
            if kw:
                raise ValueError('cannot change options when reusing a cursor')
            return just_yield(cursor)
        return super(GratipayDB, self).get_cursor(**kw)

    def self_check(self):
        with self.get_cursor() as cursor:
            check_db(cursor)


def check_db(cursor):
    """Runs all available self checks on the given cursor.
    """
    _check_balances(cursor)
    _check_no_team_balances(cursor)
    _check_tips(cursor)
    _check_orphans(cursor)
    _check_orphans_no_tips(cursor)


def _check_tips(cursor):
    """
    Checks that there are no rows in tips with duplicate (tipper, tippee, mtime).

    https://github.com/gratipay/gratipay.com/issues/1704
    """
    conflicting_tips = cursor.one("""
        SELECT count(*)
          FROM
             (
                SELECT * FROM tips
                EXCEPT
                SELECT DISTINCT ON(tipper, tippee, mtime) *
                  FROM tips
              ORDER BY tipper, tippee, mtime
              ) AS foo
    """)
    assert conflicting_tips == 0


def _check_balances(cursor):
    """
    Recalculates balances for all participants from transfers and exchanges.

    https://github.com/gratipay/gratipay.com/issues/1118
    """
    b = cursor.all("""
        select p.username, expected, balance as actual
          from (
            select username, sum(a) as expected
              from (
                      select participant as username, sum(amount) as a
                        from exchanges
                       where amount > 0
                         and (status = 'unknown' or status = 'succeeded')
                    group by participant

                       union all

                      select participant as username, sum(amount-fee) as a
                        from exchanges
                       where amount < 0
                         and (status = 'unknown' or status <> 'failed')
                    group by participant

                       union all

                      select tipper as username, sum(-amount) as a
                        from transfers
                    group by tipper

                       union all

                      select participant as username, sum(amount) as a
                        from payments
                       where direction='to-participant'
                    group by participant

                       union all

                      select participant as username, sum(-amount) as a
                        from payments
                       where direction='to-team'
                    group by participant

                       union all

                      select tippee as username, sum(amount) as a
                        from transfers
                    group by tippee
                    ) as foo
            group by username
          ) as foo2
        join participants p on p.username = foo2.username
        where expected <> p.balance
    """)
    assert len(b) == 0, "conflicting balances: {}".format(b)

def _check_no_team_balances(cursor):
    if cursor.one("select exists (select * from paydays where ts_end < ts_start) as running"):
        # payday is running
        return
    teams = cursor.all("""
        SELECT t.slug, balance
          FROM (
                SELECT team, sum(delta) as balance
                  FROM (
                        SELECT team, sum(-amount) AS delta
                          FROM payments
                         WHERE direction='to-participant'
                      GROUP BY team

                         UNION ALL

                        SELECT team, sum(amount) AS delta
                          FROM payments
                         WHERE direction='to-team'
                      GROUP BY team
                       ) AS foo
              GROUP BY team
               ) AS foo2
          JOIN teams t ON t.slug = foo2.team
         WHERE balance <> 0
    """)
    assert len(teams) == 0, "teams with non-zero balance: {}".format(teams)


def _check_orphans(cursor):
    """
    Finds participants that
        * does not have corresponding elsewhere account
        * have not been absorbed by other participant

    These are broken because new participants arise from elsewhere
    and elsewhere is detached only by take over which makes a note
    in absorptions if it removes the last elsewhere account.

    Especially bad case is when also claimed_time is set because
    there must have been elsewhere account attached and used to sign in.

    https://github.com/gratipay/gratipay.com/issues/617
    """
    orphans = cursor.all("""
        select username
           from participants
          where not exists (select * from elsewhere where elsewhere.participant=username)
            and not exists (select * from absorptions where archived_as=username)
    """)
    assert len(orphans) == 0, "missing elsewheres: {}".format(list(orphans))


def _check_orphans_no_tips(cursor):
    """
    Finds participants
        * without elsewhere account attached
        * having non zero outstanding tip

    This should not happen because when we remove the last elsewhere account
    in take_over we also zero out all tips.
    """
    orphans_with_tips = cursor.all("""
        WITH valid_tips AS (SELECT * FROM current_tips WHERE amount > 0)
        SELECT username
          FROM (SELECT tipper AS username FROM valid_tips
                UNION
                SELECT tippee AS username FROM valid_tips) foo
         WHERE NOT EXISTS (SELECT 1 FROM elsewhere WHERE participant=username)
    """)
    assert len(orphans_with_tips) == 0, orphans_with_tips

from datetime import timedelta
from unittest.mock import patch

from freezegun import freeze_time

from odoo import fields
from odoo.tests.common import TransactionCase
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.queue_job.exception import RetryableJobError


# Named "create" (not just defined as a nested/lambda) and its __name__
# forced to match: Odoo's own method-wrapping machinery introspects
# func.__name__ when a model method is invoked through the ORM's normal
# dispatch, and raises `AttributeError: '...' object has no attribute
# '<original __name__>'` if it doesn't match the attribute name it was
# assigned to (confirmed in practice — a differently-named replacement
# function breaks with exactly that error, even though patch.object
# assigns it to the right attribute).
def _raise_retryable_no_seconds(self, vals):
    raise RetryableJobError("boom")


_raise_retryable_no_seconds.__name__ = "create"


def _raise_retryable_with_seconds(self, vals):
    raise RetryableJobError("rate limited", seconds=42)


_raise_retryable_with_seconds.__name__ = "create"


@tagged("post_install", "-at_install")
class TestRetryPatternFix(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env = cls.env(context=dict(cls.env.context, tracking_disable=True))
        # A dedicated queue.job.function record for res.partner.create,
        # with a real retry_pattern — this is the exact configuration
        # shape xe_meli_connector's own job functions already use in
        # production (see xe_meli_connector/data/queue_job_data.xml).
        # 999/1999 are deliberately far from both the stock
        # queue_job_cron_jobrunner's hardcoded 5 seconds and
        # RETRY_INTERVAL's own default, so a passing assertion can only
        # mean the real retry_pattern was consulted, not a coincidence.
        model = cls.env["ir.model"]._get("res.partner")
        cls.env["queue.job.function"].create({
            "model_id": model.id, "method": "create",
            "retry_pattern": {1: 999, 3: 1999},
        })

    @mute_logger("odoo.addons.queue_job_cron_jobrunner_retry_pattern_fix.models.queue_job")
    def test_retryable_error_without_explicit_seconds_uses_retry_pattern(self):
        """The bug this module fixes: queue_job_cron_jobrunner's stock
        _process() hardcodes seconds=5 when postponing a job after a
        RetryableJobError, which Job._get_retry_seconds() treats as an
        explicit override and never falls back to retry_pattern for
        (`if not seconds and retry_pattern`). This proves the override
        makes it fall through to retry_pattern instead, for the common
        case (no explicit seconds on the exception).
        """
        with freeze_time("2026-09-10 12:00:00"):
            job = self.env["res.partner"].with_delay().create({"name": "test"})
            job_record = job.db_record()

            # A real function, not a Mock: Job.__init__ requires
            # inspect.ismethod(func) to be True (queue/queue_job/job.py's
            # own _is_model_method) — a MagicMock's side_effect breaks
            # that check ("Job accepts only methods of Models") because
            # a Mock doesn't implement the descriptor protocol the way a
            # plain function does, so accessing it through the instance
            # never produces a genuine bound method.
            with patch.object(
                type(self.env["res.partner"]), "create",
                _raise_retryable_no_seconds,
            ):
                # Calling _process() directly on this ONE job record —
                # not _job_runner(), which loops re-acquiring "ready"
                # jobs via a SQL `now()` comparison. freeze_time only
                # mocks Python's clock, never Postgres's own now(): under
                # a frozen time far in the past relative to the real
                # wall clock, _acquire_one_job()'s `eta <= now()` check
                # sees the just-postponed job as immediately overdue
                # again, and _job_runner's own `while job:` loop
                # re-processes it repeatedly within this single call,
                # silently exhausting every retry (confirmed in
                # practice: this looked like the fix itself was broken
                # — "Max. retries (5) reached" — when it was actually
                # this test running the job 5 times over instead of
                # once). Calling _process() directly processes it
                # exactly once, matching what this test actually needs
                # to verify.
                job_record._process(commit=False)

            self.assertEqual(job_record.state, "pending")
            self.assertEqual(
                job_record.eta,
                fields.Datetime.from_string("2026-09-10 12:00:00") + timedelta(seconds=999),
                "must use retry_pattern's own 999s for retry 1, not the "
                "stock runner's hardcoded 5s",
            )

    @mute_logger("odoo.addons.queue_job_cron_jobrunner_retry_pattern_fix.models.queue_job")
    def test_retryable_error_with_explicit_seconds_is_still_honored(self):
        """The other half of the fix: when the raising code DOES specify
        an explicit delay (e.g. xe_meli_connector honoring a 429's
        Retry-After header), that value must still be used exactly as
        before — retry_pattern only applies when the exception itself
        doesn't already know how long to wait.
        """
        with freeze_time("2026-09-10 12:00:00"):
            job = self.env["res.partner"].with_delay().create({"name": "test"})
            job_record = job.db_record()

            with patch.object(
                type(self.env["res.partner"]), "create",
                _raise_retryable_with_seconds,
            ):
                # Calling _process() directly on this ONE job record —
                # not _job_runner(), which loops re-acquiring "ready"
                # jobs via a SQL `now()` comparison. freeze_time only
                # mocks Python's clock, never Postgres's own now(): under
                # a frozen time far in the past relative to the real
                # wall clock, _acquire_one_job()'s `eta <= now()` check
                # sees the just-postponed job as immediately overdue
                # again, and _job_runner's own `while job:` loop
                # re-processes it repeatedly within this single call,
                # silently exhausting every retry (confirmed in
                # practice: this looked like the fix itself was broken
                # — "Max. retries (5) reached" — when it was actually
                # this test running the job 5 times over instead of
                # once). Calling _process() directly processes it
                # exactly once, matching what this test actually needs
                # to verify.
                job_record._process(commit=False)

            self.assertEqual(
                job_record.state, "pending",
                job_record.exc_info,
            )
            self.assertEqual(
                job_record.eta,
                fields.Datetime.from_string("2026-09-10 12:00:00") + timedelta(seconds=42),
                "an explicit seconds= on the exception must still be honored",
            )

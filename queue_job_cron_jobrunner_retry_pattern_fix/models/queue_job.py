import logging
import traceback
from io import StringIO

from psycopg2 import OperationalError

from odoo import _, api, models, tools
from odoo.service.model import PG_CONCURRENCY_ERRORS_TO_RETRY

from odoo.addons.queue_job.controllers.main import PG_RETRY
from odoo.addons.queue_job.exception import (
    FailedJobError,
    NothingToDoJob,
    RetryableJobError,
)
from odoo.addons.queue_job.job import Job

_logger = logging.getLogger(__name__)


class QueueJob(models.Model):
    _inherit = "queue.job"

    def _process(self, commit=False):
        """Full override of queue_job_cron_jobrunner's own _process()
        (queue/queue_job_cron_jobrunner/models/queue_job.py, version
        17.1.1.0 at the time this was copied — re-sync this method by
        hand if that module is ever upgraded and its _process() changes).

        The ONLY change from the original: the `except RetryableJobError`
        branch below passes `seconds=err.seconds` instead of a hardcoded
        `seconds=5`. queue_job's own RetryableJobError docstring
        (queue/queue_job/exception.py) is explicit about the intended
        contract: "If seconds is empty, it will be retried according to
        the retry_pattern of the job" — and Job._get_retry_seconds()
        (queue/queue_job/job.py) only ever consults a job's own
        retry_pattern when the `seconds` argument it receives is falsy
        (`if not seconds and retry_pattern:`). The stock
        queue_job_cron_jobrunner always passes the literal `5`, which is
        truthy, so it silently skips retry_pattern for EVERY job that
        ever raises RetryableJobError under this runner — not just
        Mercado Libre's, any module's — regardless of what max_retries/
        retry_pattern that job's own queue.job.function record declares.
        Confirmed in production (2026-09-10): a job configured with
        retry_pattern={1:30, 3:120, 5:600, 7:1800} was observed retrying
        every ~5 seconds instead, exhausting all 8 attempts in well under
        a minute and hammering a remote API with almost no real backoff
        between attempts.

        `err.seconds` is `None` for a RetryableJobError raised without an
        explicit delay (the common case — e.g. a plain transient 5xx),
        which correctly falls through to retry_pattern via
        _get_retry_seconds's own `if not seconds` check. `err.seconds` is
        a real number when the raising code deliberately specifies one
        (e.g. this company's own xe_meli_connector honoring a 429's
        Retry-After header) — that explicit value is honored exactly as
        before, unchanged.

        Odoo.sh does not support running queue_job's own dedicated
        JobRunner daemon (it requires a long-lived background process
        outside Odoo.sh's supported worker/cron model), which is why
        this whole stack runs on queue_job_cron_jobrunner instead — this
        override stays inside that same constraint, it does not attempt
        to switch runners.
        """
        self.ensure_one()
        job = Job._load_from_db_record(self)
        # Set it as started
        job.set_started()
        job.store()
        _logger.debug("%s started", job.uuid)
        # TODO: Commit the state change so that the state can be read from the UI
        #       while the job is processing. However, doing this will release the
        #       lock on the db, so we need to find another way.
        # if commit:
        #     self.env.flush_all()
        #     self.env.cr.commit()

        # Actual processing
        try:
            try:
                with self.env.cr.savepoint():
                    job.perform()
                    job.set_done()
                    job.store()
            except OperationalError as err:
                # Automatically retry the typical transaction serialization errors
                if err.pgcode not in PG_CONCURRENCY_ERRORS_TO_RETRY:
                    raise
                message = tools.ustr(err.pgerror, errors="replace")
                job.postpone(result=message, seconds=PG_RETRY)
                job.set_pending(reset_retry=False)
                job.store()
                _logger.debug("%s OperationalError, postponed", job)

        except NothingToDoJob as err:
            if str(err):
                msg = str(err)
            else:
                msg = _("Job interrupted and set to Done: nothing to do.")
            job.set_done(msg)
            job.store()

        except RetryableJobError as err:
            # delay the job later, requeue — see this method's own
            # docstring above for why `err.seconds` (not a hardcoded
            # value) is the fix.
            job.postpone(result=str(err), seconds=err.seconds)
            job.set_pending(reset_retry=False)
            job.store()
            _logger.debug("%s postponed", job)

        except (FailedJobError, Exception):
            with StringIO() as buff:
                traceback.print_exc(file=buff)
                _logger.error(buff.getvalue())
                job.set_failed(exc_info=buff.getvalue())
                job.store()

        if commit:  # pragma: no cover
            self.env.flush_all()
            self.env.cr.commit()  # pylint: disable=invalid-commit

        _logger.debug("%s enqueue depends started", job)
        job.enqueue_waiting()
        _logger.debug("%s enqueue depends done", job)

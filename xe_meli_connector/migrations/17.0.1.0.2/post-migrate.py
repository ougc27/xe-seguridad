import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Fix 6 (2026-09-09, final review — Important): forces
    queue.job.function 'job_function_sale_order_meli_import'
    (xe_meli_connector's own external id) to carry Task 5's
    retry_pattern on an already-deployed database, where this
    job.function record predates this whole plan and already exists
    under queue_job_data.xml's noupdate="1" data block.

    Why this is needed instead of just editing queue_job_data.xml:
    Odoo's own XML data loading never re-applies a changed field to a
    record that already exists in the database once its
    ir.model.data.noupdate flag is stored True — and that stored flag
    is never updated by any later XML declaration (confirmed by
    reading odoo/addons/base/models/ir_model.py: _update_xmlids's own
    INSERT ... ON CONFLICT DO UPDATE clause only ever touches (model,
    res_id, write_date), never noupdate). A commonly cited workaround —
    re-declaring the same external id in a second, separate
    <data noupdate="0"> block containing only the field(s) to
    force-sync — was tried first and empirically PROVEN not to work
    for this case: two consecutive `-u xe_meli_connector` runs against
    this task's own test database left retry_pattern NULL both times,
    because odoo.models.Model._load_records gates the actual field
    write on the STORED noupdate value (read fresh from the database
    at load time), not on the current tag's own noupdate attribute.

    A version-bump migration script is the one mechanism Odoo runs
    unconditionally, exactly once, for every database that upgrades
    across this exact module version boundary — regardless of any
    noupdate flag. Verified working in this task's own test database:
    after bumping __manifest__.py's version to 17.0.1.0.2 and adding
    this script, a real `-u xe_meli_connector` upgrade (starting from
    a database where the module was already installed at 17.0.1.0.1,
    with retry_pattern still NULL on this record beforehand) left
    retry_pattern correctly set afterward — confirmed by reading the
    field back from the database, not just by re-reading this script.

    Uses the ORM (not raw SQL) so the field's own JobSerialized
    (de)serialization is exercised exactly like every other write to
    this field, rather than guessing at its on-disk JSON text
    representation.
    """
    if not version:
        # No previously-installed version at all means this is a fresh
        # install, not an upgrade — queue_job_data.xml's own
        # noupdate="1" block already sets retry_pattern correctly in
        # that case (as it always has), nothing to force here.
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    job_function = env.ref(
        'xe_meli_connector.job_function_sale_order_meli_import',
        raise_if_not_found=False,
    )
    if not job_function:
        _logger.warning(
            "xe_meli_connector 17.0.1.0.2 migration: "
            "job_function_sale_order_meli_import not found — nothing "
            "to force-update (unexpected, but not fatal)."
        )
        return
    job_function.retry_pattern = {1: 30, 3: 120, 5: 600, 7: 1800}

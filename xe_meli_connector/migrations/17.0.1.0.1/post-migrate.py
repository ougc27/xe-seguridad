def migrate(cr, version):
    """data/ir_cron.xml loads with noupdate="1" — this data change
    (5h -> 2h refresh interval) never reaches a database where the
    module was already installed unless applied explicitly here. See
    docs/superpowers/specs/2026-08-31-meli-token-refresh-resilience-design.md.

    Also pulls `nextcall` forward if the existing (5h-cadence) schedule
    would otherwise fire later than the new 2h cadence allows -- without
    this, the very next run after upgrading still waits out the old
    interval once, delaying by up to 3h the "catch failures sooner"
    benefit this migration exists to deliver.
    """
    cr.execute("""
        UPDATE ir_cron
        SET interval_number = 2, interval_type = 'hours',
            nextcall = LEAST(nextcall, (now() at time zone 'utc') + interval '2 hours')
        WHERE id IN (
            SELECT res_id FROM ir_model_data
            WHERE module = 'xe_meli_connector'
            AND name = 'ir_cron_meli_refresh_tokens'
        )
    """)

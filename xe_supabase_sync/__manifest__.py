# -*- coding: utf-8 -*-
{
    'name': 'XE Supabase Sync',
    'version': '17.0.1.0.1',
    'summary': 'Syncs Odoo tables (stock.quant, sale.order, pos.order, etc.) to Supabase',
    'description': """
Configurable sync of Odoo data into dedicated tables in Supabase.

- Extraction via direct SQL, ORM, or a mix (for computed / non-stored fields).
- Transport via a direct Postgres connection (psycopg2) with batch upsert.
- Per-job checkpoint for historical backfill plus incremental sync.
- Run log for every execution, for auditing.
""",
    'author': 'Xe Brands',
    'category': 'Technical',
    'depends': ['base', 'base_setup'],
    'external_dependencies': {
        'python': ['psycopg2'],
    },
    'data': [
        'security/ir.model.access.csv',
        'views/supabase_sync_job_views.xml',
        'views/supabase_sync_log_views.xml',
        'views/supabase_sync_menus.xml',
        'views/res_config_settings_views.xml',
        'data/ir_cron_data.xml',
    ],
    'license': 'LGPL-3',
    'installable': True,
    'application': False,
}

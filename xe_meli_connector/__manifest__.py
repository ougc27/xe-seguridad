{
    'name': 'Mercado Libre Connector',
    'version': '17.0.1.0.3',
    'license': 'LGPL-3',
    'author': 'XE Brands',
    'category': 'Sales/Sales',
    'summary': 'OAuth connection, sales import, and SKU mapping with Mercado Libre',
    'description': """
Mercado Libre Connector
==========================================
Native Odoo <-> Mercado Libre integration (replaces Ventiapp).
Includes: OAuth authentication (connect the ML account to Odoo and keep the
token alive via automatic refresh), Mercado Libre SKU mapping to Odoo
products, and sales import (webhook + backup polling) that creates
sale.order records from paid Mercado Libre orders. Self-invoicing is built
on top of this same module in a later increment.
    """,
    'depends': ['base', 'product', 'sale', 'stock', 'queue_job', 'l10n_mx_edi', 'mail'],
    'external_dependencies': {'python': ['requests', 'openpyxl']},
    'data': [
        'security/meli_security_groups.xml',
        'security/ir.model.access.csv',
        'security/meli_config_security.xml',
        'views/meli_config_views.xml',
        'views/meli_sku_mapping_views.xml',
        'views/sale_order_views.xml',
        'views/sale_order_search_views.xml',
        'views/sale_order_tree_views.xml',
        'views/meli_claim_views.xml',
        'views/meli_import_batch_views.xml',
        'views/meli_invoice_document_views.xml',
        'views/meli_invoice_import_batch_views.xml',
        'wizards/meli_manual_import_wizard_views.xml',
        'wizards/meli_import_batch_wizard_views.xml',
        'wizards/meli_claim_recovery_wizard_views.xml',
        'wizards/meli_invoice_recovery_wizard_views.xml',
        'wizards/meli_invoice_import_batch_wizard_views.xml',
        'wizards/meli_delivery_recovery_wizard_views.xml',
        'data/ir_cron.xml',
        'data/queue_job_data.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}

{
    'name': 'Mercado Libre Connector (Invoicing)',
    'version': '17.0.1.0.5',
    'license': 'LGPL-3',
    'author': 'XE Brands',
    'category': 'Sales/Sales',
    'summary': 'Mercado Libre self-invoicing (facturación/notas de crédito) and SKU mapping',
    'description': """
Mercado Libre Connector — Invoicing
==========================================
Invoicing-only build (2026-09-14): OAuth authentication, Mercado Libre SKU
mapping to Odoo products, and native self-invoicing (facturas/notas de
crédito, via webhook + Excel import + backup polling) against sale orders
that already exist in Odoo. Does NOT create sale orders from Mercado Libre
on its own — no order/claim webhook or polling is wired up in this build.
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
        'views/meli_invoice_document_views.xml',
        'views/meli_invoice_import_batch_views.xml',
        'wizards/meli_invoice_recovery_wizard_views.xml',
        'wizards/meli_invoice_import_batch_wizard_views.xml',
        # 2026-09-21 (user request): temporarily hidden from the UI —
        # commenting out only the VIEW file, not the model/wizard code
        # itself (still registered via wizards/__init__.py, still
        # perfectly usable from the ORM/shell if needed). This just
        # removes its menu item and action, so nobody sees "Importar
        # Pedido" in the Mercado Libre menu for now. Uncomment this one
        # line to bring it back — nothing else needs touching.
        # 'wizards/meli_order_import_wizard_views.xml',
        'data/ir_cron.xml',
        'data/queue_job_data.xml',
    ],
    'installable': True,
    'application': True,
    'auto_install': False,
}

# -*- coding: utf-8 -*-
# © 2024 Morwi Encoders Consulting SA DE CV
# License LGPL-3.0 or later (https://www.gnu.org/licenses/lgpl.html).

{
    "name": "XE Customs",
    "version": "17.0.0.0.1",
    "license": "LGPL-3",
    "author": "XE Customs",
    'sequence': 1,
    "depends": [
        'purchase',
        'sale',
        'account',
    ],
    "category": "Accounting",
    "summary": "XE Customs",
    "data": [
        # Security
        'security/ir.model.access.csv',

        # Reports
        'reports/report_remissions.xml',

        # Views
        'views/account_views.xml',
        'views/product_views.xml',
        'views/sale_views.xml',
        'views/stock_picking.xml',
    ],
    'assets': {
        'web.assets_backend': [
            "xe_customs/static/src/js/*.js",
        ],
    },
    "demo": [],
    'installable': True,
    'auto_install': False,
}

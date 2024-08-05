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
        'sale_management',
    ],
    "category": "Accounting",
    "summary": "XE Customs",
    "data": [
        # Security
        'security/ir.model.access.csv',

        # Views
        'views/product_views.xml',
    ],
    "demo": [],
    'installable': True,
    'auto_install': False,
}

# See LICENSE file for full copyright and licensing details.

{
    "name": "MX-EDI XE Analytics",
    "version": "17.0.0.0.0",
    "license": "LGPL-3",
    "author": "XE Brands",
    'sequence': 1,
    "depends": [
        'account',
        'sale_stock',
        'purchase'
    ],
    "category": "Accounting",
    "summary": "MX-EDI XE Analytics",
    'data': [
        'views/analytic_distribution_model.xml',
    ],
    "demo": [],
    'installable': True,
    'auto_install': False,
}

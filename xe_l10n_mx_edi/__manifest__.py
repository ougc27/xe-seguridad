# See LICENSE file for full copyright and licensing details.

{
    "name": "MX-EDI XE Brands",
    "version": "17.0.0.0.11",
    "license": "LGPL-3",
    "author": "XE Brands",
    'sequence': 1,
    "depends": [
        'l10n_mx_edi_extended',
        'sale',
        'xe_pacific',
        'cloud_storage_google'
    ],
    "category": "Accounting",
    "summary": "MX-EDI XE Brands",
    "data": [
        'security/ir.model.access.csv',
        'security/res_groups.xml',
        'views/res_partner.xml',
        'views/account_move_views.xml'
    ],
    "demo": [],
    'installable': True,
    'auto_install': False,
}

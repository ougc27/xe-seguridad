# See LICENSE file for full copyright and licensing details.

{
    "name": "MX-EDI XE Brands",
    "version": "17.0.0.0.9",
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
        'views/res_partner.xml',
        'security/ir.model.access.csv',
    ],
    "demo": [],
    'installable': True,
    'auto_install': False,
}

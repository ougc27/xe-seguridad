# See LICENSE file for full copyright and licensing details.

{
    "name": "MX-EDI XE Brands",
    "version": "17.0.0.0.8",
    "license": "LGPL-3",
    "author": "XE Brands",
    'sequence': 1,
    "depends": [
        'l10n_mx_edi_extended',
        'sale',
        'xe_customs'
    ],
    "category": "Accounting",
    "summary": "MX-EDI XE Brands",
    "data": [
        'views/res_partner.xml',
    ],
    "demo": [],
    'installable': True,
    'auto_install': False,
}

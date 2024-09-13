# See LICENSE file for full copyright and licensing details.

{
    "name": "MX-EDI Pacific Rim",
    "version": "17.0.0.0.1",
    "license": "LGPL-3",
    "author": "XE Brands",
    'sequence': 1,
    "depends": [
        'base',
        'sale_project',
        'helpdesk_fsm',
        'stock'
    ],
    "category": "Accounting",
    "summary": "MX-EDI XE Brands",
    "data": [],
    #'assets': {
        #'web.assets_backend': [
            #'xe_pacific/static/src/js/attachment_preview.js',
        #],
        #'web.assets_qweb': [
            #'xe_pacific/static/src/xml/attachment_preview.xml',
        #],
    #},
    "demo": [],
    'installable': True,
    'auto_install': False,
}

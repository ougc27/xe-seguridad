{
    'name': 'Addenda HEB',
    "author": "XE Brands",
    "license" : "LGPL-3",
    'category': 'Account',
    'summary': "Addenda HEB",
    'version': '17.0.0.0.3',
    'description': """
Addendas HEB
===============================================================
This module adds the HEB addendum in signed invoices, the following 
features are adds:
- inheritance of the account.move model, required fields are added.
- added heb_branches catalog.
Also a template is added for render the addenda.
    """,
    'depends': [
        'addenda_morwi',
        'l10n_mx_edi',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/4.0/addenda.xml',
        'data/heb_branch_data.xml',
        'views/account_move_views.xml',
        'views/heb_branch_views.xml',
        'views/res_partner_views.xml',
    ],
    'demo': [],
    'qweb': [],
    'installable': True,
    'application': False,
}

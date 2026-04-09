# -*- coding: utf-8 -*-
{
    'name': 'Addenda Soriana',
    "author": "XE Brands",
    "license" : "LGPL-3",
    'category': 'Account',
    'summary': "Addenda Soriana",
    'version': '17.0.0.0.1',
    'description': """
Addendas Soriana
===============================================================
This module adds the Soriana addendum in signed invoices, the following 
features are adds:
- inheritance of the account.move model, required fields are added.
- inheritance of the res.company model, required fields are added.
- inheritance of the res.partner model, required fields are added.
Also a template is added for render the addenda.
    """,
    'depends': [
        'addenda_morwi'
    ],
    'data': [
        'data/4.0/addenda.xml',
        'views/account_move_views.xml',
        'views/res_company_views.xml',
    ],
    'installable': True,
    'application': False,
}

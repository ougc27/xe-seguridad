# -*- encoding: utf-8 -*-
#
# Module written to Odoo, Open Source Management Solution
#
# Copyright (c) 2024 Morwi - http://www.mowri.mx/
# All Rights Reserved.
#
# Developer(s): David Morales
#               (david.morales@morwi.mx)
########################################################################

{
    'name': 'Addenda HEB',
    'author': 'XE Brands',
    "license" : "LGPL-3",
    'category': 'Account',
    'version': '17.0.0.0.0',
    'description': """
Addendas HEB
===============================================================
This module adds the HEB addendum in signed invoices, the following 
features are adds:
- inheritance of the account.move model, required fields are added.
- inheritance of the res.company model, required fields are added.
- inheritance of the res.partner model, required fields are added.
Also a template is added for render the addenda.
    """,
    'depends': [
        'base',
        'l10n_mx_edi',
        'addenda_morwi'
    ],
    'data': [
        'security/ir.model.access.csv',
        #'data/addenda.xml',
        'views/account_move_views.xml',
        'views/heb_store_views.xml',
        'views/res_company_views.xml',
    ],
    'demo': [],
    'qweb': [],
    'installable': True,
    'application': False,
}

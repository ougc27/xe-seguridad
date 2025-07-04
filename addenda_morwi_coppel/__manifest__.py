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
    'name': 'Morwi | Addenda Morwi Coppel',
    'author': 'Morwi ©',
    "license" : "LGPL-3",
    'category': 'Account',
    'sequence': 50,
    'summary': "Addenda Coppel",
    'website': 'https://www.morwi.com',
    'version': '1.2',
    'description': """
Addendas Coppel
===============================================================
This module adds the Coppel addendum in signed invoices, the following 
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
        'data/4.0/addenda.xml',
        'data/coppel_store_data.xml',
        'views/account_move_view.xml',
        'views/coppel_store_views.xml',
    ],
    'demo': [],
    'qweb': [],
    'installable': True,
    'application': False,
}

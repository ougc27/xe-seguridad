{
    'name': 'Addenda HEB',
    'author': 'XE Brands',
    'license': 'LGPL-3',
    'category': 'Account',
    'summary': 'HEB Addenda – CFDI 4.0 invoice submission via SOAP 1.2',
    'version': '17.0.1.2.3',
    'description': """
Addenda HEB (AMC GS1 v7.1)
===========================
Adds the HEB addenda (requestForPayment) to CFDI 4.0 invoices and sends
them to the HEB reception web service (MexicoDigitalInvoiceService) using
SOAP 1.2 with WS-Security UsernameToken authentication.

Features
--------
- HEB branch catalog (heb.branch) with GLN.
- Buyer / seller GLN fields on the partner.
- Addenda template (requestForPayment, AMC 7.1).
- "Send to HEB" button on the invoice: stamps must be applied first.
- Full submission detail (request + response) stored in a dedicated log
  model (heb.invoice.log), visible only to the Settings group. A single
  status field on the invoice shows the state; the chatter keeps a short
  note with the acknowledgment reference.
- "Test HEB Connection" button (developer mode) using getMessage.
- Pure requests-based SOAP client; no zeep dependency.
    """,
    'depends': [
        'addenda_morwi',
        'l10n_mx_edi',
        'readonly_group_tool_cr',
    ],
    'data': [
        'security/ir.model.access.csv',
        'data/4.0/addenda.xml',
        'data/heb_branch_data.xml',
        'views/account_move_views.xml',
        'views/heb_branch_views.xml',
        'views/heb_invoice_log_views.xml',
        'views/res_partner_views.xml',
        'views/res_company_views.xml',
    ],
    'external_dependencies': {
        'python': ['requests'],
    },
    'demo': [],
    'installable': True,
    'application': False,
}

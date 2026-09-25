{
    'name': 'FIFO Customer Payment Reconcile',
    'version': '17.0.1.0.0',
    'license': 'LGPL-3',
    'author': 'XE Brands',
    'category': 'Accounting/Accounting',
    'summary': 'Apply released customer payments to the oldest open PUE invoices (FIFO) in background batches',
    'description': """
FIFO Customer Payment Reconcile
===============================
Applies each released customer payment (account.payment) against the
oldest open invoices of the same commercial partner, oldest first, until
the payment is exhausted. Runs in background batches through a scheduled
action chained with ir.cron._trigger().

- Opt-in per commercial partner (FIFO Auto-Reconcile flag).
- Payments are released manually by users of the "Release payments for
  FIFO reconcile" group.
- PUE only: invoices without a payment method or with payment method 99
  (PPD) are always excluded. Reconciling PPD invoices would require
  payment complements (CFDI de pago) and is out of scope.
- Batch size: system parameter
  xe_fifo_payment_reconcile.fifo_reconcile_batch_size (default 250).

See README.md for setup and states.
    """,
    'depends': ['account', 'mail', 'l10n_mx_edi'],
    'data': [
        'security/fifo_reconcile_security.xml',
        'views/res_partner_views.xml',
        'views/account_payment_views.xml',
    ],
    'installable': True,
    'application': False,
    'auto_install': False,
}

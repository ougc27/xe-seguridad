from odoo import fields, models


class AccountMove(models.Model):
    _inherit = 'account.move'
    
    soriana_store_number = fields.Char(string="Store Number")
    soriana_delivery_place_number = fields.Integer(
        string="Delivery Place Number",
        help="Indicates the number corresponding to the place where the goods will be delivered. "
             "Use '1' when delivering directly to the store (shopping centers or City Club branches). "
             "If delivered to a distribution center (CEDIS), the value depends on the specific CEDIS.",
        default=1
    )
    soriana_delivery_date = fields.Datetime(
        string="Delivery Date",
        help="Date of the appointment for merchandise delivery."
    )
    soriana_appointment_number = fields.Char(
        string="Appointment Number",
        help="Unique number that identifies the delivery appointment."
    )
    soriana_order_folio = fields.Char(
        string="Order Folio",
        help="Customer or supplier order folio."
    )

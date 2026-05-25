from odoo import models, fields


class ResPartner(models.Model):
    _inherit = 'res.partner'
    
    addenda_heb_buyer_gln = fields.Char(string='HEB Buyer GLN',
        help='Global Location Number of the buyer, used in HEB addenda.')
    addenda_heb_seller_gln = fields.Char(string='HEB Seller GLN',
        help='Global Location Number of the seller, used in HEB addenda.')
    addenda_heb_alternate_party_identification = fields.Char(string='HEB Alternate Party Identification',
        help='Seller assigned identifier for a party, used in HEB addenda.')

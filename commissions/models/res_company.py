# coding: utf-8

from odoo import api, fields, models, _


class ResCompany(models.Model):
    _inherit = 'res.company'

    def cron_commission_sequence(self):
        company_ids = self.search([])
        for company_id in company_ids:
            company_id.create_sequence()

    def create_sequence(self):
        sequence_ids = self.env['ir.sequence'].search([
            ('code', '=', 'xe.payment.commission'),
            ('company_id', '=', self.id)
        ])
        if not sequence_ids:
            self.env['ir.sequence'].create({
                'name': _('Commission payment sequence'),
                'code': 'xe.payment.commission',
                'prefix': 'COM/',
                'padding': 5,
                'company_id': self.id,
            })

        sequence_ids = self.env['ir.sequence'].search([
            ('code', '=', 'xe.mass.payment.commission'),
            ('company_id', '=', self.id)
        ])
        if not sequence_ids:
            self.env['ir.sequence'].create({
                'name': _('Mass commission payment sequence'),
                'code': 'xe.mass.payment.commission',
                'prefix': 'COM-BATCH/',
                'padding': 4,
                'company_id': self.id,
            })

    @api.model_create_multi
    def create(self, val_list):
        company_ids = super(ResCompany, self).create(val_list)
        for company_id in company_ids:
            company_id.create_sequence()
        return company_ids

# -*- coding: utf-8 -*-

from odoo import models, fields, api
from odoo.exceptions import UserError


class RecalculoComision(models.TransientModel):
    _name = 'recalculo.comision'

    fch_inicial = fields.Date(
        string='Fecha inicial',
    )
    fch_final = fields.Date(
        string='Fecha final',
    )
    agente_ids = fields.Many2many(
        comodel_name='xe.agente',
        string='Agentes',
    )

    def action_recalculo_comision(self):
        move_ids = self.env['account.move'].search([
            ('move_type', '=', 'out_invoice'),
            ('invoice_date', '>=', self.fch_inicial),
            ('invoice_date', '<=', self.fch_final),
            ('state', '=', 'posted'),
        ])

        ids_agente = self.agente_ids.ids
        for move_id in move_ids:
            agente_revision_ids = self.env['xe.agente']
            if move_id.agente1_id:
                agente_revision_ids |= move_id.agente1_id
            if move_id.agente2_id:
                agente_revision_ids |= move_id.agente2_id
            if move_id.partner_id.agente1_id:
                agente_revision_ids |= move_id.partner_id.agente1_id
            if move_id.partner_id.agente2_id:
                agente_revision_ids |= move_id.partner_id.agente2_id
            ids_agente_revision = agente_revision_ids.ids
            ids_agente_revisar = set(ids_agente) & set(ids_agente_revision)
            if ids_agente_revisar:
                for id_agente_revisar in ids_agente_revisar:

                    # AGENTE 1
                    if move_id.agente1_id.id == id_agente_revisar or move_id.partner_id.agente1_id.id == id_agente_revisar:
                        if move_id.agente1_id != move_id.partner_id.agente1_id or move_id.por_agente1 != move_id.partner_id.por_agente1:
                            move_id.write({
                                'agente1_id': move_id.partner_id.agente1_id.id,
                                'por_agente1': move_id.partner_id.por_agente1,
                            })

                        move_id.line_ids.sale_line_ids.order_id.write({
                            'agente1_id': move_id.partner_id.agente1_id.id,
                            'por_agente1': move_id.partner_id.por_agente1,
                        })

                        reconciled_lines = move_id.line_ids.mapped('matched_credit_ids') + move_id.line_ids.mapped('matched_debit_ids')
                        payment_lines = reconciled_lines.mapped('credit_move_id') + reconciled_lines.mapped('debit_move_id')
                        payment_ids = payment_lines.mapped('payment_id')

                        for payment_id in payment_ids:
                            comision_ids = move_id.comision_ids.filtered(
                                lambda o: o.payment_id == payment_id and
                                          o.posicion == 1
                            )
                            if comision_ids:
                                comision_no_pagada_ids = comision_ids.filtered(
                                    lambda o: not o.pagado
                                )
                                if comision_no_pagada_ids:
                                    if move_id.agente1_id:
                                        for comision_no_pagada_id in comision_no_pagada_ids:
                                            if comision_no_pagada_id.agente_id != move_id.agente1_id or comision_no_pagada_id.por_agente != move_id.por_agente1:
                                                comision_no_pagada_id.write({
                                                    'agente_id': move_id.agente1_id.id,
                                                    'comision': comision_no_pagada_id.cobrado * move_id.por_agente1 / 100,
                                                    'por_agente': move_id.por_agente1,
                                                })
                                    else:
                                        comision_no_pagada_ids.unlink()
                            else:
                                for line_id in payment_id.line_ids:
                                    for matched_debit_id in line_id.matched_debit_ids:
                                        cobrado = matched_debit_id.amount / 1.16
                                        if move_id.agente1_id and move_id.por_agente1:
                                            agente = move_id.agente1_id.id
                                            comision = cobrado * move_id.por_agente1 / 100
                                            self.crear_comision(agente, move_id, cobrado, comision, payment_id.id, 1, move_id.por_agente1)

                    # AGENTE 2
                    if move_id.agente2_id.id == id_agente_revisar or move_id.partner_id.agente2_id.id == id_agente_revisar:
                        if move_id.agente2_id != move_id.partner_id.agente2_id or move_id.por_agente2 != move_id.partner_id.por_agente2:
                            move_id.write({
                                'agente2_id': move_id.partner_id.agente2_id.id,
                                'por_agente2': move_id.partner_id.por_agente2,
                            })

                        move_id.line_ids.sale_line_ids.order_id.write({
                            'agente2_id': move_id.partner_id.agente2_id.id,
                            'por_agente2': move_id.partner_id.por_agente2,
                        })

                        reconciled_lines = move_id.line_ids.mapped('matched_credit_ids') + move_id.line_ids.mapped('matched_debit_ids')
                        payment_lines = reconciled_lines.mapped('credit_move_id') + reconciled_lines.mapped('debit_move_id')
                        payment_ids = payment_lines.mapped('payment_id')

                        for payment_id in payment_ids:
                            comision_ids = move_id.comision_ids.filtered(
                                lambda o: o.payment_id == payment_id and
                                          o.posicion == 2
                            )
                            if comision_ids:
                                comision_no_pagada_ids = comision_ids.filtered(
                                    lambda o: not o.pagado
                                )
                                if comision_no_pagada_ids:
                                    if move_id.agente2_id:
                                        for comision_no_pagada_id in comision_no_pagada_ids:
                                            if comision_no_pagada_id.agente_id != move_id.agente2_id or comision_no_pagada_id.por_agente != move_id.por_agente2:
                                                comision_no_pagada_id.write({
                                                    'agente_id': move_id.agente2_id.id,
                                                    'comision': comision_no_pagada_id.cobrado * move_id.por_agente2 / 100,
                                                    'por_agente': move_id.por_agente2,
                                                })
                                    else:
                                        comision_no_pagada_ids.unlink()
                            else:
                                for line_id in payment_id.line_ids:
                                    for matched_debit_id in line_id.matched_debit_ids:
                                        cobrado = matched_debit_id.amount / 1.16
                                        if move_id.agente2_id and move_id.por_agente2:
                                            agente = move_id.agente2_id.id
                                            comision = cobrado * move_id.por_agente2 / 100
                                            self.crear_comision(agente, move_id, cobrado, comision, payment_id.id, 2, move_id.por_agente2)

    def crear_comision(self, id_agente, move_id, cobrado, comision, id_payment, posicion, por_agente):
        payment_id = self.env['account.payment'].browse(id_payment)
        comision_id = self.env['xe.comisiones'].create({
            'posicion': posicion,
            'agente_id': id_agente,
            'fecha': move_id.invoice_date,
            'move_id': move_id.id,
            'fecha_cobro': payment_id.date,
            'cliente_id': move_id.partner_id.id,
            'cobrado': cobrado,
            'currency_id': payment_id.currency_id.id,
            'comision': comision,
            'pagado': False,
            'sale_order_ids': [(6, 0, move_id.line_ids.sale_line_ids.order_id.ids)],
            'payment_id': id_payment,
            'por_agente': por_agente,
        })
        move_id.write({
            'comision_ids': [(4, comision_id.id)]
        })

# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from odoo import models


class Module(models.Model):
    _inherit = "ir.module.module"

    def button_install(self):
        user = self.env.user
        self.env['ir.logging'].sudo().create({
            'name': 'Module Install',
            'type': 'server',
            'dbname': self.env.cr.dbname,
            'level': 'INFO',
            'message': f'Usuario {user.name} ({user.login}) está instalando el módulo {self.name}',
            'path': 'ir.module.module.button_install',
            'func': 'button_install',
            'line': '0',
        })
        return super(Module, self).button_install()

    def button_uninstall(self):
        user = self.env.user
        self.env['ir.logging'].sudo().create({
            'name': 'Module Uninstall',
            'type': 'server',
            'dbname': self.env.cr.dbname,
            'level': 'INFO',
            'message': f'Usuario {user.name} ({user.login}) está desinstalando el módulo {self.name}',
            'path': 'ir.module.module.button_uninstall',
            'func': 'button_uninstall',
            'line': '0',
        })
        return super(Module, self).button_uninstall()

    def button_upgrade(self):
        user = self.env.user
        self.env['ir.logging'].sudo().create({
            'name': 'Module Upgraded',
            'type': 'server',
            'dbname': self.env.cr.dbname,
            'level': 'INFO',
            'message': f'Usuario {user.name} ({user.login}) está actualizando el módulo {self.name}',
            'path': 'ir.module.module.button_upgrade',
            'func': 'button_upgrade',
            'line': '0',
        })
        return super(Module, self).button_upgrade()

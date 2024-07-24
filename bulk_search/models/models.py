from odoo import api, models, _
from odoo.tools import Query
from odoo.osv import expression


class BaseModel(models.AbstractModel):
    _inherit = 'base'

    # TODO: ameliorer avec NULL
    @api.model
    def _where_calc(self, domain, active_test=True):
        """Computes the WHERE clause needed to implement an OpenERP domain.

        :param list domain: the domain to compute
        :param bool active_test: whether the default filtering of records with
            ``active`` field set to ``False`` should be applied.
        :return: the query expressing the given domain as provided in domain
        :rtype: Query
        """
        # if the object has an active field ('active', 'x_active'), filter out all
        # inactive records unless they were explicitly asked for
        if self._active_name and active_test and self._context.get('active_test', True):
            # the item[0] trick below works for domain items and '&'/'|'/'!'
            # operators too
            if not any(item[0] == self._active_name for item in domain):
                domain = [(self._active_name, '=', 1)] + domain

        is_bulk_search_active = self.env['ir.config_parameter'].sudo().get_param(
            'bulk_search_activate', None)
        if self == self.env['stock.picking']:
            for element in domain:
                if element[0] == 'name':
                    concat = [('x_studio_related_field_B0EXH', 'ilike', element[2])]
                    domain = expression.OR([domain, concat])
                    break
        if domain:
            if is_bulk_search_active == "1":
                modified_domain = []
                for domain_tuple in domain:
                    if type(domain_tuple) in (list, tuple):
                        if str(domain_tuple[1]) == 'ilike':
                            multi_name = domain_tuple[2].split('|')
                            len_name = len(multi_name)
                            if  len_name > 1:
                                for length in multi_name:
                                    modified_domain.append('|')
                                for f_name in multi_name:
                                    modified_domain.append([domain_tuple[0],domain_tuple[1],f_name.strip()])
                    modified_domain.append(domain_tuple)
                return expression.expression(modified_domain, self).query  
            else:
                return expression.expression(domain, self).query
        else:
            return Query(self.env.cr, self._table, self._table_query)

from typing import Dict, List

from odoo.fields import Command
from odoo.models import NewId
from odoo.tools.misc import OrderedSet
from odoo import api, models, fields, _
from odoo.tools import Query
from odoo.osv import expression


class BaseModel(models.AbstractModel):
    _inherit = 'base'

    def web_read(self, specification: Dict[str, Dict]) -> List[Dict]:
        fields_to_read = list(specification) or ['id']

        if fields_to_read == ['id']:
            # if we request to read only the ids, we have them already so we can build the return dictionaries immediately
            # this also avoid a call to read on the co-model that might have different access rules
            values_list = [{'id': id_} for id_ in self._ids]
        else:
            values_list: List[Dict] = self.read(fields_to_read, load=None)

        if not values_list:
            return values_list

        def cleanup(vals: Dict) -> Dict:
            """ Fixup vals['id'] of a new record. """
            if not vals['id']:
                vals['id'] = getattr(vals['id'], 'origin', False) or False
            return vals

        for field_name, field_spec in specification.items():
            field = self._fields.get(field_name)
            if field is None:
                continue

            if field.type == 'many2one':
                if 'fields' not in field_spec:
                    for values in values_list:
                        if isinstance(values[field_name], NewId):
                            values[field_name] = values[field_name].origin
                    continue

                co_records = self[field_name]
                if 'context' in field_spec:
                    co_records = co_records.with_context(**field_spec['context'])

                extra_fields = dict(field_spec['fields'])
                extra_fields.pop('display_name', None)

                many2one_data = {
                    vals['id']: cleanup(vals)
                    for vals in co_records.web_read(extra_fields)
                }

                if 'display_name' in field_spec['fields']:
                    for rec in co_records.sudo():
                        many2one_data[rec.id]['display_name'] = rec.display_name

                for values in values_list:
                    if values[field_name] is False:
                        continue
                    vals = many2one_data[values[field_name]]
                    values[field_name] = vals['id'] and vals

            elif field.type in ('one2many', 'many2many'):
                if not field_spec:
                    continue

                co_records = self[field_name]

                if 'order' in field_spec and field_spec['order']:
                    co_records = co_records.search([('id', 'in', co_records.ids)], order=field_spec['order'])
                    order_key = {
                        co_record.id: index
                        for index, co_record in enumerate(co_records)
                    }
                    for values in values_list:
                        # filter out inaccessible corecords in case of "cache pollution"
                        values[field_name] = [id_ for id_ in values[field_name] if id_ in order_key]
                        values[field_name] = sorted(values[field_name], key=order_key.__getitem__)

                if 'context' in field_spec:
                    co_records = co_records.with_context(**field_spec['context'])

                if 'fields' in field_spec:
                    if field_spec.get('limit') is not None:
                        limit = field_spec['limit']
                        ids_to_read = OrderedSet(
                            id_
                            for values in values_list
                            for id_ in values[field_name][:limit]
                        )
                        co_records = co_records.browse(ids_to_read)

                    x2many_data = {
                        vals['id']: vals
                        for vals in co_records.web_read(field_spec['fields'])
                    }

                    for values in values_list:
                        values[field_name] = [x2many_data.get(id_) or {'id': id_} for id_ in values[field_name]]

            elif field.type in ('reference', 'many2one_reference'):
                if not field_spec:
                    continue

                values_by_id = {
                    vals['id']: vals
                    for vals in values_list
                }
                for record in self:
                    if not record[field_name]:
                        continue

                    if field.type == 'reference':
                        co_record = record[field_name]
                    else:  # field.type == 'many2one_reference'
                        co_record = self.env[record[field.model_field]].browse(record[field_name])

                    if 'context' in field_spec:
                        co_record = co_record.with_context(**field_spec['context'])

                    if 'fields' in field_spec:
                        reference_read = co_record.web_read(field_spec['fields'])
                        if any(fname != 'id' for fname in field_spec['fields']):
                            # we can infer that if we can read fields for the co-record, it exists
                            co_record_exists = bool(reference_read)
                        else:
                            co_record_exists = co_record.exists()
                    else:
                        # If there are no fields to read (field_spec.get('fields') --> None) and we web_read ids, it will
                        # not actually read the records so we do not know if they exist.
                        # This ensures the record actually exists
                        co_record_exists = co_record.exists()

                    record_values = values_by_id[record.id]

                    if not co_record_exists:
                        record_values[field_name] = False
                        if field.type == 'many2one_reference':
                            record_values[field.model_field] = False
                        continue

                    if 'fields' in field_spec:
                        record_values[field_name] = reference_read[0]
                        if field.type == 'reference':
                            record_values[field_name]['id'] = {
                                'id': co_record.id,
                                'model': co_record._name
                            }

        return values_list

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
                    if '|' in element[2]:
                        search_terms = tuple(element[2].split('|'))  
                        query = Query(self.env.cr, self._table, self._table_query)
                        query.add_where('"x_studio_related_field_B0EXH" IN %s', (search_terms,))
                        return query
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

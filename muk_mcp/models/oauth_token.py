import hashlib
import secrets

from odoo import api, fields, models

from odoo.addons.muk_mcp.tools.rate_limit import rate_limiter


class MCPOAuthToken(models.Model):

    _name = 'muk_mcp.oauth.token'
    _description = "MCP OAuth Token"
    _order = 'create_date desc'

    # ----------------------------------------------------------
    # Fields
    # ----------------------------------------------------------

    name = fields.Char(
        string="Label",
        readonly=True,
    )

    access_token_hash = fields.Char(
        string="Access Token Hash",
        readonly=True,
        index=True,
    )

    refresh_token_hash = fields.Char(
        string="Refresh Token Hash",
        readonly=True,
        index=True,
    )

    client_id = fields.Many2one(
        comodel_name='muk_mcp.oauth.client',
        string="Client",
        required=True,
        readonly=True,
        ondelete='cascade',
    )

    user_id = fields.Many2one(
        comodel_name='res.users',
        string="User",
        required=True,
        readonly=True,
        index=True,
        ondelete='cascade',
    )

    scope = fields.Selection(
        selection=[
            ('read', "Read Only"),
            ('write', "Read & Write"),
        ],
        string="Scope",
        required=True,
        default='write',
        groups='base.group_system',
        help="Only System users can view or change this. Regular users "
             "get a fixed scope when they connect and can't grant "
             "themselves write access.",
    )

    rate_limit = fields.Integer(
        string="Rate Limit (req/min)",
        default=60,
        readonly=True,
    )

    expires_at = fields.Datetime(
        string="Access Token Expires",
        readonly=True,
    )

    refresh_expires_at = fields.Datetime(
        string="Refresh Token Expires",
        readonly=True,
    )

    revoked = fields.Boolean(
        string="Revoked",
        default=False,
    )

    last_used = fields.Datetime(
        string="Last Used",
        readonly=True,
    )

    create_date = fields.Datetime(
        string="Issued",
        readonly=True,
    )

    # ----------------------------------------------------------
    # Helper
    # ----------------------------------------------------------

    @staticmethod
    def _hash_token(token):
        return hashlib.sha256(token.encode()).hexdigest()

    def _check_rate_limit(self, count=1):
        return rate_limiter.check(
            f'muk_mcp.oauth.token:{self.id}', self.rate_limit, 60, count=count,
        )

    def action_revoke(self):
        self.write({'revoked': True})

    # ----------------------------------------------------------
    # Functions
    # ----------------------------------------------------------

    @api.model
    def authenticate(self, token):
        record = self.sudo().search([
            ('access_token_hash', '=', self._hash_token(token)),
            ('revoked', '=', False),
            ('expires_at', '>', fields.Datetime.now()),
        ], limit=1)
        if not record:
            return None
        record.write({'last_used': fields.Datetime.now()})
        return record

    @api.model
    def _issue(self, client, user, scope):
        rate_limit = int(self.env['ir.config_parameter'].sudo().get_param(
            'muk_mcp.rate_limit_requests', 60,
        ))
        access_minutes = int(self.env['ir.config_parameter'].sudo().get_param(
            'muk_mcp.oauth_access_token_minutes', 60,
        ))
        refresh_days = int(self.env['ir.config_parameter'].sudo().get_param(
            'muk_mcp.oauth_refresh_token_days', 30,
        ))
        access_token = secrets.token_urlsafe(32)
        refresh_token = secrets.token_urlsafe(32)
        now = fields.Datetime.now()
        record = self.sudo().create({
            'name': client.client_name,
            'client_id': client.id,
            'user_id': user.id,
            'scope': scope,
            'rate_limit': rate_limit,
            'access_token_hash': self._hash_token(access_token),
            'refresh_token_hash': self._hash_token(refresh_token),
            'expires_at': fields.Datetime.add(now, minutes=access_minutes),
            'refresh_expires_at': fields.Datetime.add(now, days=refresh_days),
        })
        return {
            'record': record,
            'access_token': access_token,
            'refresh_token': refresh_token,
            'expires_in': access_minutes * 60,
        }

    @api.model
    def _refresh(self, refresh_token, client_id):
        record = self.sudo().search([
            ('refresh_token_hash', '=', self._hash_token(refresh_token)),
            ('revoked', '=', False),
            ('refresh_expires_at', '>', fields.Datetime.now()),
        ], limit=1)
        if not record or record.client_id.client_id != client_id:
            return None
        # Rotate: the old refresh token can only be redeemed once.
        record.write({'revoked': True})
        return self._issue(record.client_id, record.user_id, record.scope)

    # ----------------------------------------------------------
    # Cron
    # ----------------------------------------------------------

    @api.autovacuum
    def _autovacuum_tokens(self):
        limit = fields.Datetime.subtract(fields.Datetime.now(), days=1)
        self.search([('refresh_expires_at', '<', limit)]).unlink()

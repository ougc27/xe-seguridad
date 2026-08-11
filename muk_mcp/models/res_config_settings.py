from odoo import fields, models


class ResConfigSettings(models.TransientModel):

    _inherit = 'res.config.settings'

    # ----------------------------------------------------------
    # Fields
    # ----------------------------------------------------------

    mcp_session_timeout = fields.Integer(
        string="Session Timeout (hours)",
        config_parameter='muk_mcp.session_timeout_hours',
        default=24,
        help="Inactive MCP sessions are cleaned up after this many hours.",
    )

    mcp_log_retention = fields.Integer(
        string="Log Retention (days)",
        config_parameter='muk_mcp.log_autovacuum_days',
        default=30,
        help="Audit logs older than this many days are automatically deleted.",
    )

    mcp_rate_limit_requests = fields.Integer(
        string="Rate Limit (Requests)",
        config_parameter='muk_mcp.rate_limit_requests',
        default=60,
        help="Default maximum MCP requests per minute per key. "
             "Set to 0 to disable. Used as default when generating new keys.",
    )

    mcp_annotate_messages = fields.Boolean(
        string="Annotate Messages",
        config_parameter='muk_mcp.annotate_messages',
        default=True,
        help=(
            "When enabled, chatter messages from MCP operations are "
            "marked to distinguish AI-originated changes from manual ones."
        ),
    )

    mcp_oauth_enabled = fields.Boolean(
        string="Enable OAuth",
        config_parameter='muk_mcp.oauth_enabled',
        default=False,
        help=(
            "Allows AI clients that support OAuth 'custom connectors' "
            "(e.g. Claude Desktop/Claude.ai) to connect by URL only, "
            "with the user logging in through Odoo instead of pasting "
            "a bearer key into a config file."
        ),
    )

    mcp_oauth_access_token_minutes = fields.Integer(
        string="Access Token Lifetime (minutes)",
        config_parameter='muk_mcp.oauth_access_token_minutes',
        default=60,
        help="How long an OAuth access token stays valid before the "
             "client must use its refresh token to get a new one.",
    )

    mcp_oauth_refresh_token_days = fields.Integer(
        string="Refresh Token Lifetime (days)",
        config_parameter='muk_mcp.oauth_refresh_token_days',
        default=30,
        help="How long an OAuth refresh token stays valid. After this "
             "period the user must reconnect and log in again.",
    )

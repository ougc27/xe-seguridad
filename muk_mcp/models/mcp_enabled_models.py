from odoo import _, api, fields, models
from odoo.exceptions import ValidationError


class McpEnabledModel(models.Model):
    """Model-level access restrictions for MCP.

    This is a blocklist, not an allowlist: a model with no record here
    is fully open to MCP (subject to the connection's own read/write
    scope). Add a record only for models you want to restrict or lock
    down entirely (deactivate it to block the model completely,
    regardless of the connection's scope).
    """

    _name = "mcp.enabled.model"
    _description = "MCP Enabled Model"
    _rec_name = "model_id"

    model_id = fields.Many2one(
        "ir.model",
        string="Model",
        required=True,
        index=True,
        ondelete="cascade",
        help="The Odoo model this restriction applies to",
    )
    model_name = fields.Char(
        related="model_id.model", string="Technical Name", store=True, readonly=True
    )
    active = fields.Boolean(
        default=True,
        help="Uncheck to fully block this model in MCP, regardless of "
             "the read/write permissions below.",
    )
    allow_read = fields.Boolean(default=True, help="Allow read operations through MCP")
    allow_create = fields.Boolean(
        default=False, help="Allow create operations through MCP"
    )
    allow_write = fields.Boolean(
        string="Allow Update", default=False, help="Allow update operations through MCP"
    )
    allow_unlink = fields.Boolean(
        string="Allow Delete", default=False,
        help="Allow delete operations through MCP for this model. "
             "Unlike the other permissions, delete is blocked by "
             "default for every model, even with no record here at "
             "all — you must opt a model in explicitly by checking "
             "this box.",
    )
    notes = fields.Text(help="Additional notes about this model configuration")

    _sql_constraints = [
        (
            "unique_model",
            "UNIQUE(model_id)",
            "A model can only be enabled once for MCP access.",
        )
    ]

    @api.model
    def is_model_enabled(self, model_name):
        """Check if a model is allowed for MCP access.

        Blocklist semantics: a model with no restriction record is
        allowed. A record only matters if it exists — deactivate it to
        fully block the model, regardless of the allow_* flags below.

        Args:
            model_name (str): The technical name of the model

        Returns:
            bool: True if the model is allowed, False if it's blocked
        """
        record = self.search([("model_name", "=", model_name)], limit=1)
        if not record:
            return True
        return record.active

    @api.model
    def check_model_operation_enabled(self, model_name, operation):
        """Check if a specific operation is allowed for a model.

        Blocklist semantics for read/create/write: with no restriction
        record (or none matching), the operation is allowed. Once a
        record exists for the model, its allow_* flags are
        authoritative for that model and are enforced regardless of
        the connection's own read/write scope (that scope is checked
        separately, on top of this).

        Deletion is the one exception and is NOT open by default: an
        MCP client must never be able to delete records unless a
        system admin has explicitly opted that specific model in via
        allow_unlink=True. A model with no record, or an inactive
        record, is BLOCKED for delete (the opposite of read/create/
        write).

        Args:
            model_name (str): The technical name of the model
            operation (str): One of 'read', 'create', 'write', 'unlink'

        Returns:
            bool: True if the operation is allowed, False if blocked

        Raises:
            ValidationError: If an invalid operation is specified
        """
        if operation not in ["read", "create", "write", "unlink"]:
            raise ValidationError(_("Invalid operation: %s") % operation)

        record = self.search([("model_name", "=", model_name)], limit=1)

        if operation == "unlink":
            return bool(record and record.active and record.allow_unlink)

        if not record:
            return True
        if not record.active:
            return False

        return bool(record["allow_" + operation])

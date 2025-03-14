/** @odoo-module */

import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";

registry.category("discuss.channel_commands").add("person", {
    channel_types: ["whatsapp"],
    help: _t("Assigned person in the current channel"),
    methodName: "execute_command_person",
});

/** @odoo-module **/

import { Component } from "@odoo/owl";
import { useService } from "@web/core/utils/hooks";
import { registry } from "@web/core/registry";
import { session } from '@web/session';

export class CreateTicket extends Component {

    static template = "xe_pacific.create_ticket";

    setup() {
        this.action = useService("action");
        this.ui = useService("ui");
    }

    async onClick() {
        await this.action.doAction("xe_pacific.action_create_ticket_wizard", {
            additionalContext: {
                'env_values': {
                    'current_url': window.location.href,
                    'origin_url': window.location.origin,
                    'username': session.username,
                    'partner_name': session.partner_display_name,
                    'current_context': session.user_context,
                }
            }
        });
    }
}

export const createTicket = {
    Component: CreateTicket,
};

registry
    .category("systray")
    .add("xe_pacific.create_ticket", createTicket, { sequence: 101 });

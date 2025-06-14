/** @odoo-module **/

import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Component, onWillStart, useState } from "@odoo/owl";

export class UserAvailableButton extends Component {

    static template = "xe_whatsapp.user_available_button";

    setup() {
        this.orm = useService("orm");
        this.rpc = useService("rpc");
        this.user = useService("user");
        this.state = useState({
            online: false,
            canShowButton: false,
        });
        onWillStart(async () => {
            this.state.canShowButton = await this.user.hasGroup("xe_whatsapp.can_see_available_button_group");
            const [data] = await this.orm.read("res.users", [this.user.userId], ["is_available"]);
            this.state.online = data.is_available;
        });
    }

    async toggleStatus() {
        this.state.online = !this.state.online;
        await this.rpc("/xe_whatsapp/user/sudo_write_is_available", {
            user_id: this.user.userId,
            is_available: this.state.online
        });
    }

    get iconColor() {
        return this.state.online ? "green" : "red";
    }

    get tooltip() {
        return this.state.online ? "Disponible para conversaciones de whatsapp" : "No disponible";
    }
}

export const userAvailableButton= {
    Component: UserAvailableButton,
};

registry
    .category("systray")
    .add("xe_whatsapp.UserAvailableButton", userAvailableButton, { sequence: 102 });

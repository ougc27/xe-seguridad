/* @odoo-module */
import { Component, useState, onWillStart } from "@odoo/owl";
import { jsonrpc } from "@web/core/network/rpc_service";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";

const { onMounted } = owl;

//CONFIGURACIÓN
const TARGET_DAY = 0;              // Domingo
const TARGET_HOUR = 23;            // 11 PM
const CHECK_INTERVAL = 5 * 60 * 1000;  // Revisa cada 5 minutos
const LAST_LOGOUT_KEY = "auto_logout_last_forced";

class BiWeeklyLogout extends Component {
    static template = "xe_auto_logout.BiWeeklyLogout";

    setup() {
        super.setup();
        this.user = useService("user");
        this.state = useState({ 
            active: true,
            groupSystem: false,
            groupAccountManager: false
        });

        onMounted(async () => {
            await this.check_if_admin();
        });

        onWillStart(async () => {
            this.state.groupSystem = await this.user.hasGroup("base.group_system");
            this.state.groupAccountManager = await this.user.hasGroup("account.group_account_manager");            
        });
    }

    async check_if_admin() {

        if (this.state.groupSystem || this.state.groupAccountManager) {
            this.state.active = false;
            return;
        }

        this.start_logout_checker();
    }

    start_logout_checker() {
        setInterval(() => {
            if (!this.state.active) return;
            if (this.should_logout_now()) {
                this.force_logout();
            }
        }, CHECK_INTERVAL);
    }

    should_logout_now() {
        const now = new Date();
        const lastLogout = localStorage.getItem(LAST_LOGOUT_KEY);

        /*if (lastLogout && lastLogout === now.toDateString()) {
            return false;
        }*/

        const isTargetDay = now.getDay() === TARGET_DAY;
        const isTargetHour = now.getHours() === TARGET_HOUR;

        return isTargetDay && isTargetHour;
    }

    async force_logout() {
        localStorage.setItem(LAST_LOGOUT_KEY, new Date().toDateString());
        location.replace("/web/session/logout")
    }
}

export const systrayItem = {
    Component: BiWeeklyLogout,
};

registry.category("systray").add(
    "xe_auto_logout.BiWeeklyLogout",
    systrayItem,
    { sequence: 25 }
);

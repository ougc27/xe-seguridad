/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks"
import { useState } from "@odoo/owl";
import { SetSaleOrderButton } from "@pos_sale/app/set_sale_order_button/set_sale_order_button";

patch(SetSaleOrderButton.prototype, {

    setup() {
        super.setup(...arguments);
        this.orm = useService("orm");
        this.state = useState({
            isAdvancedUser: false,
        });
        this.getPosPermissions(this.pos.user.id);
    },
    
    async getPosPermissions(user_id) {
        const hasAdvancePermission = await this.orm.call("res.users", "get_pos_permission", [], {
            user_id: user_id,
        });
        this.state.isAdvancedUser = hasAdvancePermission;
    },
});

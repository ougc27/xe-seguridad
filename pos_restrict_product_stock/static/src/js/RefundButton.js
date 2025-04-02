/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks"
import { useState } from "@odoo/owl";
import { RefundButton } from "@point_of_sale/app/screens/product_screen/control_buttons/refund_button/refund_button";

patch(RefundButton.prototype, {

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

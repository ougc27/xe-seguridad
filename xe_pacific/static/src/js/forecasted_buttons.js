/** @odoo-module **/

import { ForecastedButtons } from '@stock/stock_forecasted/forecasted_buttons';
import { useService } from "@web/core/utils/hooks";
import { patch } from "@web/core/utils/patch";
import { onWillStart, useState } from "@odoo/owl";


patch(ForecastedButtons.prototype, {
    setup() {
        super.setup(...arguments);
        this.user = useService("user");
        this.state = useState({
            canShowReplenish: false,
            canShowUpdateQuantity: false,
        });
        onWillStart(async () => {
            this.state.canShowReplenish = await this.user.hasGroup("base.group_system");
            this.state.canShowUpdateQuantity = await this.user.hasGroup("base.group_system");
        });
    },
});

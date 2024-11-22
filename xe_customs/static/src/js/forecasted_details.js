/** @odoo-module **/

import { ForecastedDetails } from "@stock/stock_forecasted/forecasted_details";
import { patch } from "@web/core/utils/patch";
import { useService } from "@web/core/utils/hooks";

patch(ForecastedDetails.prototype, {
    setup() {
        super.setup(...arguments);
        this.rpc = useService("rpc");
    },

    async displayReserve(line) {
        const picking = await this.rpc(
            "/xe_customs/forecast/get_picking_locked",
            {
                picking_id: line.move_out.picking_id.id,
            },
        );

        console.log(line);
        console.log(picking.is_locked);

        return super.displayReserve(line) && !picking.is_locked;
    }
});

/*
patch(ForecastedDetails.prototype, {
    displayReserve(line) {
        console.log(line)
        return super.displayReserve(line) && !line.move_out.is_locked;
    }
});
*/
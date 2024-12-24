/** @odoo-module **/
import { ForecastedDetails } from "@stock/stock_forecasted/forecasted_details";
import { patch } from "@web/core/utils/patch";

patch(ForecastedDetails.prototype, {
    displayReserve(line) {
        console.log(line)
        return super.displayReserve(line) && !line.move_out.is_locked;
    }
});

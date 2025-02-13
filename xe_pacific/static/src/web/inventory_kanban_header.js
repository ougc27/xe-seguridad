/** @odoo-module **/

import { KanbanHeader } from "@web/views/kanban/kanban_header";
import { patch } from "@web/core/utils/patch";

patch(KanbanHeader.prototype, {
    setup() {
        super.setup();
    },

    get doorCount() {
        if (this.props.group._config.resModel == 'stock.picking') {
            const door_count = this.props.group.aggregates.door_count;
            if (door_count) {
                return door_count;
            }
            return 0;
        }
    }
});

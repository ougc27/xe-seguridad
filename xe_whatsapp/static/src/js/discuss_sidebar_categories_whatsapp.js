/** @odoo-module **/

import { DiscussSidebarCategories } from "@mail/discuss/core/web/discuss_sidebar_categories";
import { patch } from "@web/core/utils/patch";
import { onExternalClick } from "@mail/utils/common/hooks";
import { useState } from "@odoo/owl";
import { useBus, useService } from "@web/core/utils/hooks";


patch(DiscussSidebarCategories.prototype, {
    setup() {
        super.setup();
        this.state = useState({
            editing: false,
            search: false,
            quickSearchVal: "",
            openPerson: false,
            groups: [],
        });
        onExternalClick("selector", () => {
            this.state.search = false;
            this.state.openPerson = false;
        });
        //useBus(this.env.bus, "whatsapp:assigned_person_updated", (ev) => this._changeAssignedState(ev.detail));
    },

    _changeAssignedState(options) {
        const { channelId, personId, thread} = options;
        this.groupedByAssignedPerson(thread);
    },

    searchCategory(category) {
        this.state.search = category.id;
        category.search = true;
    },

    addToCategory(category) {
        this.state.editing = category.id;
        category.search = false;
    },

    stopEditing() {
        this.state.editing = false;
        this.state.search = false;
    },

    groupedByAssignedPerson(threads) {
        const groups = {};
        for (const thread of threads) {
            const assignedPerson = thread.assigned_person ? thread.assigned_person[1] : 'Sin asignar';
            if (!groups[assignedPerson]) {
                groups[assignedPerson] = { assigned_person_name: assignedPerson, isOpen: false, threads: [] };
            }
            groups[assignedPerson].threads.push(thread);
        }
        this.state.groups = Object.values(groups)
        return this.state.groups;
    },

    toggleAssignedPerson(assigned_group) {
        this.state.openPerson = !this.state.openPerson;
    }
});

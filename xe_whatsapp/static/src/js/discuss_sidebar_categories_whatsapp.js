/** @odoo-module **/

import { DiscussSidebarCategories } from "@mail/discuss/core/web/discuss_sidebar_categories";
import { patch } from "@web/core/utils/patch";
import { onExternalClick } from "@mail/utils/common/hooks";
import { useState } from "@odoo/owl";


patch(DiscussSidebarCategories.prototype, {
    setup() {
        super.setup();
        this.state = useState({
            editing: false,
            search: false,
            quickSearchVal: "",
            openPerson: null,
            groups: [],
        });
        onExternalClick("selector", () => {
            this.state.search = false;
            this.state.openPerson = null;
        });
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

    groupedByAssignedPerson(category) {
        let threads = this.filteredThreads(category);
        const groups = {};
        threads = [...threads].sort((a, b) => {
            return (b.is_reassigned === true) - (a.is_reassigned === true);
        });
        for (const thread of threads) {
            const assignedPerson = thread.assigned_person ? thread.assigned_person[1] : "Sin asignar";
            if (!groups[assignedPerson]) {
                groups[assignedPerson] = {
                    assigned_person_name: assignedPerson,
                    isOpen: this.state.openPerson === assignedPerson, // Solo se abre el seleccionado
                    threads: []
                };
            }
            groups[assignedPerson].threads.push(thread);
        }
        this.state.groups = Object.values(groups);
        return this.state.groups;
    },

    toggleAssignedPerson(person) {
        this.state.openPerson = this.state.openPerson === person ? null : person;
    }
});

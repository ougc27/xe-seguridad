/* @odoo-module */

import { Component, useState, onWillStart } from "@odoo/owl";
import { _t } from "@web/core/l10n/translation";
import { useService } from "@web/core/utils/hooks";

export class ChannelAssignedPerson extends Component {
    static template = "discuss.ChannelAssignedPerson";
    static props = ["thread", "className?"];

    setup() {
        this.rpc = useService("rpc");
        this.store = useState(useService("mail.store"));
        this.threadService = useState(useService("mail.thread"));
        this.state = useState({
            assignedPerson: null,
            availablePersons: [],
        });

        onWillStart(async () => {
            await this.fetchAssignedPerson();
        });
    }

    async fetchAssignedPerson() {
        if (!this.props.thread || !this.props.thread.id) {
            return;
        }

        const result = await this.rpc("/xe_whatsapp/get_assigned_persons", {
            channel_id: this.props.thread.id,
        });

        if (result) {
            this.state.assignedPerson = result.assigned_person;
            this.state.availablePersons = result.available_persons.filter(
                person => person.id !== result.assigned_person?.id
            );        
        }
    }

    async assignPerson(personId) {
        const thread = this.props.thread;
        const channelId = thread.id

        if (!thread || !thread.id) {
            return;
        }

        const success = await this.rpc("/xe_whatsapp/set_assigned_person", {
            channel_id: channelId,
            person_id: personId,
        });

        if (success) {
            this.state.assignedPerson = this.state.availablePersons.find(p => p.id === personId);
            const person = [this.state.assignedPerson.id, this.state.assignedPerson.name];
            thread.assigned_person = person
            this.fetchAssignedPerson();
        }
    }
}
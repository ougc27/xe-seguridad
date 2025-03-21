/* @odoo-module */

import { threadActionsRegistry } from "@mail/core/common/thread_actions";
//export const threadActionsRegistry = registry.category("mail.thread/actions");
import { useService } from "@web/core/utils/hooks";
import { ChannelAssignedPerson } from "@xe_whatsapp/js/assigned_person";
import { _t } from "@web/core/l10n/translation";

threadActionsRegistry.add("assigned-person-list", {
    component: ChannelAssignedPerson,
    condition(component) {
        return (
            component.thread?.model === "discuss.channel" &&
            (!component.props.chatWindow || component.props.chatWindow.isOpen)
        );
    },
    panelOuterClass: "o-discuss-ChannelMemberList",
    icon: "fa fa-fw fa-user-circle",
    iconLarge: "fa fa-fw fa-lg fa-user-circle",
    name: _t("Show Assigned Person List"),
    nameActive: _t("Hide Assigned Person List"),
    sequence: 5,
    toggle: true,
});
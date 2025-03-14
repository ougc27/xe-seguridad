/** @odoo-module **/

import { ChannelSelector } from "@mail/discuss/core/web/channel_selector";
import { cleanTerm } from "@mail/utils/common/format";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";
import { registry } from "@web/core/registry";


patch(ChannelSelector.prototype, {
    props: {
        ...ChannelSelector.props,
    },
    async fetchSuggestions() {
        const cleanedTerm = cleanTerm(this.state.value);
        if (this.props.category.id === "whatsapp" && cleanedTerm) {
            let results = false
            if (this.props.category.search) {
                if (cleanedTerm.length < 4) {
                    return;
                }
                results = await this.sequential(() =>
                    this.orm.call("whatsapp.message", "get_messages", [], {
                        content: cleanedTerm,
                    })
                );
            } 
            else {
                const domain = [
                    ["channel_type", "=", "whatsapp"],
                    ["name", "ilike", cleanedTerm],
                ];
                const model = "discuss.channel";
                const field = ["name"];
                results = await this.sequential(() =>
                    this.orm.searchRead(model, domain, field, {
                        limit: 10,
                    })
                );
            }

            if (!results) {
                this.state.navigableListProps.options = [];
                return;
            }
            const choices = results.map((channel) => {
                const label = channel.name;
                return {
                    channelId: channel.id,
                    classList: "o-mail-ChannelSelector-suggestion",
                    label: channel.name,
                };
            });
            if (choices.length === 0) {
                choices.push({
                    classList: "o-mail-ChannelSelector-suggestion",
                    label: _t("No results found"),
                    unselectable: true,
                });
            }
            this.state.navigableListProps.options = choices;
            return;
        }
        return super.fetchSuggestions();
    },

    onSelect(option) {
        if (this.props.category.id === "whatsapp") {
            this.threadService.openWhatsAppChannel(option.channelId, option.label);
            this.onValidate();
        } else {
            super.onSelect(option);
        }
    },
    get inputPlaceholder() {
        let label = this.props.category.addTitle;
        if (this.props.category.search) {
            label = _t("Search by messages");
        }
        return this.state.selectedPartners.length > 0
            ? _t("Press Enter to start")
            : label;
    }
});

/*export class ChannelSelectorWhatsapp extends ChannelSelector {
    static template = "xe_whatsapp.ChannelSelectorWhatsapp";
    static props = {
        ...ChannelSelector.props,
        search: { type: Boolean, optional: true },
    };

    async fetchSuggestions() {
        const cleanedTerm = cleanTerm(this.state.value);
        if (this.props.category.id === "whatsapp" && cleanedTerm) {
            let results = false
            if (this.props.category.search) {
                if (cleanedTerm.length < 4) {
                    return;
                }
                results = await this.sequential(() =>
                    this.orm.call("whatsapp.message", "get_messages", [], {
                        content: cleanedTerm,
                    })
                );
            } 
            else {
                const domain = [
                    ["channel_type", "=", "whatsapp"],
                    ["name", "ilike", cleanedTerm],
                ];
                const model = "discuss.channel";
                const field = ["name"];
                results = await this.sequential(() =>
                    this.orm.searchRead(model, domain, field, {
                        limit: 10,
                    })
                );
            }

            if (!results) {
                this.state.navigableListProps.options = [];
                return;
            }
            const choices = results.map((channel) => {
                const label = channel.name;
                return {
                    channelId: channel.id,
                    classList: "o-mail-ChannelSelector-suggestion",
                    label: channel.name,
                };
            });
            if (choices.length === 0) {
                choices.push({
                    classList: "o-mail-ChannelSelector-suggestion",
                    label: _t("No results found"),
                    unselectable: true,
                });
            }
            this.state.navigableListProps.options = choices;
            return;
        }
        return super.fetchSuggestions();
    }

    onSelect(option) {
        if (this.props.category.id === "whatsapp") {
            this.threadService.openWhatsAppChannel(option.channelId, option.label);
            this.onValidate();
        } else {
            super.onSelect(option);
        }
    }

    get inputPlaceholder() {
        const label = this.props.category.addTitle;
        if (this.props.search) {
            label = _t("Search by messages");
        }
        return this.state.selectedPartners.length > 0
            ? _t("Press Enter to start")
            : label;
    }
}

registry.category("fields").add("xe_whatsapp.channel_selector_whatsapp", ChannelSelectorWhatsapp);*/
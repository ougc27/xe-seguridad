/** @odoo-module **/

import { ChannelSelector } from "@mail/discuss/core/web/channel_selector";
import { cleanTerm } from "@mail/utils/common/format";
import { patch } from "@web/core/utils/patch";
import { _t } from "@web/core/l10n/translation";


patch(ChannelSelector.prototype, {
    props: {
        ...ChannelSelector.props,
        'searchByTerm': false
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
});

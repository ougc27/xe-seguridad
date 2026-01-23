/* @odoo-module */

import { Thread } from "@mail/core/common/thread_model";
import { assignDefined, assignIn } from "@mail/utils/common/misc";
import { patch } from "@web/core/utils/patch";


patch(Thread.prototype, {
    update(data) {
        const { id, name, attachments, description, assigned_person, wa_account_id, ...serverData } = data;
        assignDefined(this, { id, name, description, assigned_person, wa_account_id });
        if (attachments) {
            this.attachments = attachments;
        }
        if (serverData) {
            assignDefined(this, serverData, [
                "uuid",
                "authorizedGroupFullName",
                "avatarCacheKey",
                "description",
                "hasWriteAccess",
                "is_pinned",
                "isLoaded",
                "isLoadingAttachments",
                "mainAttachment",
                "message_unread_counter",
                "message_needaction_counter",
                "message_unread_counter_bus_id",
                "name",
                "seen_message_id",
                "state",
                "type",
                "status",
                "group_based_subscription",
                "last_interest_dt",
                "custom_notifications",
                "mute_until_dt",
                "is_editable",
                "defaultDisplayMode",
                "assigned_person",
                'is_reassigned',
                'wa_account_id',
            ]);
            assignIn(this, data, [
                "custom_channel_name",
                "memberCount",
                "channelMembers",
                "invitedMembers",
            ]);
            if ("channel_type" in data) {
                this.type = data.channel_type;
            }
            if ("channelMembers" in data) {
                if (this.type === "chat") {
                    for (const member of this.channelMembers) {
                        if (
                            member.persona.notEq(this._store.user) ||
                            (this.channelMembers.length === 1 &&
                                member.persona?.eq(this._store.user))
                        ) {
                            this.chatPartner = member.persona;
                        }
                    }
                }
            }
            if ("seen_partners_info" in serverData) {
                this._store.ChannelMember.insert(
                    serverData.seen_partners_info.map(
                        ({ id, fetched_message_id, partner_id, guest_id, seen_message_id }) => ({
                            id,
                            persona: {
                                id: partner_id ?? guest_id,
                                type: partner_id ? "partner" : "guest",
                            },
                            lastFetchedMessage: fetched_message_id
                                ? { id: fetched_message_id }
                                : undefined,
                            lastSeenMessage: seen_message_id ? { id: seen_message_id } : undefined,
                        })
                    )
                );
            }
        }
        if (this.type === "channel") {
            this._store.discuss.channels.threads.add(this);
        } else if (this.type === "chat" || this.type === "group") {
            this._store.discuss.chats.threads.add(this);
        }
        if (!this.type && !["mail.box", "discuss.channel"].includes(this.model)) {
            this.type = "chatter";
        }
        if (this.type === "whatsapp") {
            assignDefined(this, data, ["whatsapp_channel_valid_until"]);
            if (!this._store.discuss.whatsapp.threads.includes(this)) {
                this._store.discuss.whatsapp.threads.push(this);
            }
        }
    }
});

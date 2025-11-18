/** @odoo-module */
import { Chatter } from "@mail/core/web/chatter";

import { patch } from "@web/core/utils/patch";
import { session } from "@web/session";

patch(Chatter.prototype, {
    setup() {
        super.setup();
        this.cloudStorageUsable = session.cloud_storage_min_file_size !== undefined;
    },
});

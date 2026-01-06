/** @odoo-module **/

import { AttachmentList } from "@mail/core/common/attachment_list";
import { _t } from "@web/core/l10n/translation";
import { patch } from "@web/core/utils/patch";


patch(AttachmentList.prototype, {
    getActions(attachment) {
        const res = [];
        if (this.canDownload(attachment)) {
            res.push({
                label: _t("Download"),
                icon: "fa fa-download",
                onSelect: () => this.onClickDownload(attachment),
            });
        }
        return res;
    },
    async onClickDownload(attachment) {
        const url = attachment.downloadUrl;
        const match = url.match(/\/content\/(\d+)/);
        const attachmentId = match ? parseInt(match[1]) : null;
        const data = await this.orm.call(
            "ir.attachment",
            "read",
            [[attachmentId], ["type"]]
        );
    
        const type = data?.[0]?.type;
        const isCloud = type === "cloud_storage";
        if (isCloud) {
            window.open(url, "_blank");
            return;
        }
        const downloadLink = document.createElement("a");
        downloadLink.setAttribute("href", url);
        // Adding 'download' attribute into a link prevents open a new
        // tab or change the current location of the window. This avoids
        // interrupting the activity in the page such as rtc call.
        downloadLink.setAttribute("download", "");
        downloadLink.click();
    }
});

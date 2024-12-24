/** @odoo-module **/

import { registry } from "@web/core/registry";
import { Many2ManyBinaryField, many2ManyBinaryField } from "@web/views/fields/many2many_binary/many2many_binary_field";
import { useService } from "@web/core/utils/hooks";
import { FileInput } from "@web/core/file_input/file_input";
import { Dialog } from "@web/core/dialog/dialog";
import { Component, xml } from "@odoo/owl";

const style = document.createElement('style');
style.textContent = `
    .imagem2m-preview-dialog {
        text-align: center;
    }
    .imagem2m-preview-dialog img {
        max-width: 100%;
        max-height: 80vh;
        object-fit: contain;
    }
`;
document.head.appendChild(style);

class M2MImagePreviewDialog extends Component {
    static template = xml`
        <Dialog title="'Image Preview'">
            <div class="imagem2m-preview-dialog">
                <img t-att-src="props.imageUrl" alt="Image Preview"/>
            </div>
            <t t-set-slot="footer">
                <button class="btn btn-primary" t-on-click="() => props.close()">Close</button>
            </t>
        </Dialog>
    `;
    static components = { Dialog };
    static props = ["close", "imageUrl"];
}

class Many2ManyBinaryPreviewField extends Many2ManyBinaryField {
    static template = "web.Many2ManyBinaryPreviewField";
    static components = { ...Many2ManyBinaryField.components, FileInput, M2MImagePreviewDialog };

    setup() {
        super.setup();
        this.dialogService = useService("dialog");
    }

    openPreview(file) {
        const fileUrl = this.getUrl(file.id);
        if (this.isImage(file)) {
            this.dialogService.add(M2MImagePreviewDialog, {
                imageUrl: fileUrl,
            });
        } else {
            window.open(fileUrl, '_blank');
        }
    }

    isImage(file) {
        return file.mimetype && file.mimetype.startsWith('image/');
    }

    onFileClicked(file, ev) {
        ev.preventDefault();
        this.openPreview(file);
    }

    getUrl(id) {
        return `/web/content/${id}`;
    }

    downloadFile(file) {
        const url = this.getUrl(file.id);
        const link = document.createElement('a');
        link.href = url;
        link.download = file.name;
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
    }
}

export const many2manybinarypreviewfield = {
    ...many2ManyBinaryField,
    component: Many2ManyBinaryPreviewField,
};

registry.category("fields").add("many2many_binary_preview", many2manybinarypreviewfield);
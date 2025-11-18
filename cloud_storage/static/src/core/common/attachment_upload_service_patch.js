/** @odoo-module */
import { AttachmentUploadService } from "@mail/core/common/attachment_upload_service";
import { patch } from "@web/core/utils/patch";
import { session } from "@web/session";
import { _t } from "@web/core/l10n/translation";
import { Deferred } from "@web/core/utils/concurrency";

let nextId = -1;

patch(AttachmentUploadService.prototype, {
    setup(env, services) {
        this.env = env;
        this.fileUploadService = services["file_upload"];
        /** @type {import("@mail/core/common/store_service").Store} */
        this.store = services["mail.store"];
        this.notificationService = services["notification"];
        /** @type {import("@mail/core/common/attachment_service").AttachmentService} */
        this.attachmentService = services["mail.attachment"];

        this.abortByAttachmentId = new Map();
        this.deferredByAttachmentId = new Map();
        this.uploadingAttachmentIds = new Set();
        this.hookersByTmpId = new Map();
        this.uploadingCloudFiles = new Map();
        this.uploadedAttachmentByTmpId = new Map(); 

        this.fileUploadService.bus.addEventListener("FILE_UPLOAD_ADDED", ({ detail: { upload } }) => {
            const tmpId = parseInt(upload.data.get("temporary_id"));
            if (!this.uploadingAttachmentIds.has(tmpId)) {
                return;
            }
            if (this.store.Attachment.get(tmpId)) {
                return;
            }

            const hooker = this.hookersByTmpId.get(tmpId);
            const threadId = parseInt(upload.data.get("thread_id"));
            const threadModel = upload.data.get("thread_model");
            const tmpUrl = upload.data.get("tmp_url");
            const originThread = this.store.Thread.insert({
                model: threadModel,
                id: threadId,
            });

            this.abortByAttachmentId.set(tmpId, upload.xhr.abort.bind(upload.xhr));
            const attachment = this.store.Attachment.insert(
                this._makeAttachmentData(
                    upload,
                    tmpId,
                    hooker?.composer ? undefined : originThread,
                    tmpUrl
                )
            );

            attachment.tmpId = tmpId;

            if (hooker?.composer) {
                hooker.composer.attachments.push(attachment);
            }
        });

        this.fileUploadService.bus.addEventListener("FILE_UPLOAD_LOADED", ({ detail: { upload } }) => {
            const tmpId = parseInt(upload.data.get("temporary_id"));
            if (!this.uploadingAttachmentIds.has(tmpId)) {
                return;
            }

            this.uploadingAttachmentIds.delete(tmpId);
            this.abortByAttachmentId.delete(tmpId);

            const hooker = this.hookersByTmpId.get(tmpId);

            if (upload.xhr.status === 413) {
                this.notificationService.add(_t("File too large"), { type: "danger" });
                this.hookersByTmpId.delete(tmpId);
                return;
            }
            if (upload.xhr.status !== 200) {
                this.notificationService.add(_t("Server error"), { type: "danger" });
                this.hookersByTmpId.delete(tmpId);
                return;
            }

            const response = JSON.parse(upload.xhr.response);
            if (response.error) {
                this.notificationService.add(response.error, { type: "danger" });
                this.hookersByTmpId.delete(tmpId);
                return;
            }

            const threadId = parseInt(upload.data.get("thread_id"));
            const threadModel = upload.data.get("thread_model");
            const originThread = this.store.Thread.get({ model: threadModel, id: threadId });

            const attachment = this.store.Attachment.insert({
                ...response,
                extension: upload.title.split(".").pop(),
                originThread: hooker?.composer ? undefined : originThread,
                tmpId: tmpId, // 🔥 también se conserva tmpId en el attachment final
            });

            this.uploadedAttachmentByTmpId.set(tmpId, attachment);

            const attachmentData = response?.result ?? response;
            const def = this.deferredByAttachmentId.get(tmpId);

            this._processLoaded(threadId, hooker?.composer, attachmentData, tmpId, def);
        });

        this.fileUploadService.bus.addEventListener("FILE_UPLOAD_ERROR", ({ detail: { upload } }) => {
            const tmpId = parseInt(upload.data.get("temporary_id"));
            if (!this.uploadingAttachmentIds.has(tmpId)) {
                return;
            }
            this.abortByAttachmentId.delete(tmpId);
            this.deferredByAttachmentId.delete(tmpId);
            this.uploadingAttachmentIds.delete(tmpId);
            this.hookersByTmpId.delete(tmpId);
            this.uploadingCloudFiles.delete(tmpId);
            this.uploadedAttachmentByTmpId.delete(tmpId);
        });

        window.addEventListener('beforeunload', () => this.abortByAttachmentId.forEach(abort => abort()));
    },

    // ========================
    // FUNCIONES CORE
    // ========================

    cleanProcessLoaded(hooker, attachment, tmpId, def) {
        console.log("entre en el cleanProcessLoaded");
        let finalAttachment = attachment;
        if (!finalAttachment) {
            finalAttachment = this.uploadedAttachmentByTmpId.get(tmpId) || this.store.Attachment.get(tmpId);
        }

        // 🔥 Reemplazo correcto usando tmpId
        if (hooker?.composer) {
            const index = hooker.composer.attachments.findIndex(a => a.tmpId === tmpId);
            if (index >= 0) {
                hooker.composer.attachments[index] = finalAttachment;
            } else {
                hooker.composer.attachments.push(finalAttachment);
            }
        }

        if (def) {
            def.resolve(finalAttachment);
            this.deferredByAttachmentId.delete(tmpId);
        }

        hooker?.onFileUploaded?.();
        this.hookersByTmpId.delete(tmpId);
        this.uploadingCloudFiles.delete(tmpId);
        this.uploadedAttachmentByTmpId.delete(tmpId);
        this.uploadingAttachmentIds.delete(tmpId);
        console.log("pase todo este pedo");
    },

    async _processLoaded(thread, composer, { data, upload_info }, tmpId, def) {
        if (!upload_info) {
            this.cleanProcessLoaded(...arguments);
            return;
        }

        const fileReal = this.uploadingCloudFiles.get(tmpId);
        if (!fileReal) {
            console.error("No fileReal for tmpId:", tmpId, "upload_info:", upload_info);
            def?.resolve();
            this.deferredByAttachmentId.delete(tmpId);
            this.cleanProcessLoaded(composer ? { composer } : {}, this.uploadedAttachmentByTmpId.get(tmpId), tmpId, def);
            return;
        }

        const xhr = new window.XMLHttpRequest();
        this.abortByAttachmentId.set(tmpId, xhr.abort.bind(xhr));

        xhr.open(upload_info.method, upload_info.url);
        for (const [key, value] of Object.entries(upload_info.headers || {})) {
            try {
                xhr.setRequestHeader(key, value);
            } catch (e) {
                console.warn("setRequestHeader failed for", key, value, e);
            }
        }

        xhr.onload = () => {
            if (xhr.status === upload_info.response_status) {
                this.cleanProcessLoaded(composer ? { composer } : {}, this.uploadedAttachmentByTmpId.get(tmpId), tmpId, def);
            } else if (xhr.status === 403) {
                this.notificationService.add(_t("You are not allowed to upload file to the cloud storage"), { type: "danger" });
                this.cleanProcessLoaded(composer ? { composer } : {}, this.uploadedAttachmentByTmpId.get(tmpId), tmpId, def);
            } else {
                this.notificationService.add(_t("Cloud storage error"), { type: "danger" });
                this.cleanProcessLoaded(composer ? { composer } : {}, this.uploadedAttachmentByTmpId.get(tmpId), tmpId, def);
            }
            this.deferredByAttachmentId.delete(tmpId);
        };

        xhr.onerror = () => {
            this.notificationService.add(_t("Cloud storage error"), { type: "danger" });
            this.cleanProcessLoaded(composer ? { composer } : {}, this.uploadedAttachmentByTmpId.get(tmpId), tmpId, def);
            this.deferredByAttachmentId.delete(tmpId);
        };

        xhr.onabort = () => {
            this.cleanProcessLoaded(composer ? { composer } : {}, this.uploadedAttachmentByTmpId.get(tmpId), tmpId, def);
            this.deferredByAttachmentId.delete(tmpId);
        };

        xhr.send(fileReal);
    },

    async unlink(attachment, tmpId=false) {
        super.unlink(attachment);
        this.uploadingCloudFiles.delete(tmpId);
    },

    async uploadFile(hooker, file, options) {
        const tmpId = nextId--;
        this.uploadingCloudFiles.set(tmpId, file);

        const dummyFile = new File([new Blob([])], file.name, { type: file.type });
        options = options ? { ...options, cloud_storage: true } : { cloud_storage: true };

        this.hookersByTmpId.set(tmpId, hooker);
        this.uploadingAttachmentIds.add(tmpId);

        try {
            await this.fileUploadService.upload(this.uploadURL, [dummyFile], {
                buildFormData: (formData) => {
                    this._makeFormData(formData, dummyFile, hooker, tmpId, options);
                },
            });
        } catch (e) {
            if (e.name !== "AbortError") {
                throw e;
            }
        }

        const uploadDoneDeferred = new Deferred();
        this.deferredByAttachmentId.set(tmpId, uploadDoneDeferred);
        return uploadDoneDeferred;
    },

    _buildFormData(formData, tmpURL, thread, composer, tmpId, options) {
        super._buildFormData(...arguments);
        if (options?.cloud_storage) {
            formData.append("cloud_storage", true);
        }
        return formData;
    },
});

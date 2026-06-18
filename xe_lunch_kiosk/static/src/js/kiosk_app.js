/** @odoo-module **/

import {
    App,
    Component,
    useState,
    useRef,
    onMounted,
    onWillUnmount,
    whenReady,
} from "@odoo/owl";
import { makeEnv, startServices } from "@web/env";
import { templates } from "@web/core/assets";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { MainComponentsContainer } from "@web/core/main_components_container";
import { isBarcodeScannerSupported } from "@web/webclient/barcode/barcode_scanner";
import { BarcodeScanner } from "@barcodes/components/barcode_scanner";


export class LunchBarcodeScanner extends BarcodeScanner {
    get facingMode() {
        return "user";
    }
}
LunchBarcodeScanner.props = { ...BarcodeScanner.props };

export class LunchKiosk extends Component {
    static template = "xe_lunch_kiosk.LunchKiosk";
    static components = { MainComponentsContainer, LunchBarcodeScanner };
    static props = {};

    static SCAN_DEBOUNCE_DELAY = 300;
    static MIN_BARCODE_LENGTH = 4;
    static FEEDBACK_DISPLAY_DELAY = 4000;
    static FOCUS_INTERVAL = 3000;
    static DATE_REFRESH_INTERVAL = 60000;

    setup() {
        this.rpc = useService("rpc");
        this.inputRef = useRef("barcodeInput");

        this.cameraSupported = isBarcodeScannerSupported();

        this.state = useState({
            feedback: {
                visible: false,
                type: "",
                title: "",
                subMessage: "",
            },
            currentDate: "",
        });

        this.debounceTimer = null;
        this.feedbackTimer = null;
        this.focusInterval = null;
        this.dateInterval = null;

        onMounted(() => {
            this._updateDate();
            this.dateInterval = setInterval(
                () => this._updateDate(),
                LunchKiosk.DATE_REFRESH_INTERVAL
            );

            this._onDocumentInteraction = () => this._focusInput();
            document.addEventListener("click", this._onDocumentInteraction);
            document.addEventListener("keydown", this._onDocumentInteraction);

            this.focusInterval = setInterval(
                () => this._focusInput(),
                LunchKiosk.FOCUS_INTERVAL
            );

            this._focusInput();
        });

        onWillUnmount(() => {
            clearTimeout(this.debounceTimer);
            clearTimeout(this.feedbackTimer);
            clearInterval(this.focusInterval);
            clearInterval(this.dateInterval);

            if (this._onDocumentInteraction) {
                document.removeEventListener("click", this._onDocumentInteraction);
                document.removeEventListener("keydown", this._onDocumentInteraction);
            }
        });
    }

    _updateDate() {
        const now = new Date();
        this.state.currentDate = now.toLocaleDateString("es-MX", {
            weekday: "long",
            year: "numeric",
            month: "long",
            day: "numeric",
        });
    }

    _focusInput() {
        this.inputRef.el?.focus();
    }

    onBarcodeScanned(barcode) {
        this._processBarcode(barcode);
    }

    onInputKeydown(ev) {
        clearTimeout(this.debounceTimer);

        if (ev.key === "Enter") {
            const barcode = ev.target.value;
            ev.target.value = "";
            this._processBarcode(barcode);
            return;
        }

        this.debounceTimer = setTimeout(() => {
            const barcode = ev.target.value;
            if (barcode.length > LunchKiosk.MIN_BARCODE_LENGTH) {
                ev.target.value = "";
                this._processBarcode(barcode);
            }
        }, LunchKiosk.SCAN_DEBOUNCE_DELAY);
    }

    async _processBarcode(barcode) {
        if (!barcode || !barcode.trim()) {
            return;
        }

        try {
            const result = await this.rpc("/lunch/kiosk/scan", {
                barcode: barcode.trim(),
            });

            if (result.status === "ok") {
                this._showFeedback("ok", `✅ ${result.name}`, _t("Successful registration"));
            } else if (result.status === "already_registered") {
                this._showFeedback(
                    "warning",
                    `⚠️ ${result.name}`,
                    _t("Already registered today")
                );
            } else {
                this._showFeedback(
                    "error",
                    _t("❌ Not found"),
                    _t("Barcode not recognized")
                );
            }
        } catch {
            this._showFeedback(
                "error",
                _t("❌ Connection error"),
                _t("Check your network and try again")
            );
        }
    }

    _showFeedback(type, title, subMessage) {
        clearTimeout(this.feedbackTimer);

        this.state.feedback.visible = true;
        this.state.feedback.type = type;
        this.state.feedback.title = title;
        this.state.feedback.subMessage = subMessage;

        this.feedbackTimer = setTimeout(() => {
            this.state.feedback.visible = false;
        }, LunchKiosk.FEEDBACK_DISPLAY_DELAY);
    }
}

export async function createLunchKiosk(doc) {
    await whenReady();
    const target = doc.querySelector(".o_lunch_kiosk_app");
    if (!target) {
        return;
    }
    const env = makeEnv();
    await startServices(env);
    const app = new App(LunchKiosk, {
        templates,
        env,
        dev: env.debug,
        translateFn: _t,
    });
    return app.mount(target);
}

export default { LunchKiosk, createLunchKiosk };

/** @odoo-module **/

import {
    App,
    Component,
    useState,
    useRef,
    onMounted,
    onWillStart,
    onWillUnmount,
    whenReady,
    status,
} from "@odoo/owl";
import { makeEnv, startServices } from "@web/env";
import { templates } from "@web/core/assets";
import { useService } from "@web/core/utils/hooks";
import { _t } from "@web/core/l10n/translation";
import { MainComponentsContainer } from "@web/core/main_components_container";
import { browser } from "@web/core/browser/browser";
import { delay } from "@web/core/utils/concurrency";
import { loadJS } from "@web/core/assets";
import { isVideoElementReady, buildZXingBarcodeDetector } from "@web/webclient/barcode/ZXingBarcodeDetector";
import { CropOverlay } from "@web/webclient/barcode/crop_overlay";


// ---------------------------------------------------------------------------
// KioskCamera
//
// Inline camera component that opens the device camera immediately on mount
// and continuously scans for barcodes using the BarcodeDetector API (with
// ZXing as fallback), mirroring the logic of Odoo's BarcodeDialog but
// rendered inline — no tap required, no Dialog wrapper.
// ---------------------------------------------------------------------------
export class KioskCamera extends Component {
    static template = "xe_lunch_kiosk.KioskCamera";
    static components = { CropOverlay };
    static props = {
        facingMode:       { type: String },          // 'user' | 'environment'
        onBarcodeScanned: { type: Function },
        onError:          { type: Function },
    };

    /** How often (ms) to poll the video frame for barcodes. */
    static DETECT_INTERVAL_MS = 100;

    setup() {
        this.videoRef   = useRef("video");
        this.stream     = null;
        this.detector   = null;
        this.interval   = null;
        this.overlayInfo = {};
        this.zoomRatio  = 1;

        this.state = useState({ isReady: false });

        // Build barcode detector before the component mounts so it is ready
        // the moment the video stream starts.
        onWillStart(async () => {
            let DetectorClass;
            if ("BarcodeDetector" in window) {
                DetectorClass = BarcodeDetector; // eslint-disable-line no-undef
            } else {
                await loadJS("/web/static/lib/zxing-library/zxing-library.js");
                DetectorClass = buildZXingBarcodeDetector(window.ZXing);
            }
            const formats = await DetectorClass.getSupportedFormats();
            this.detector = new DetectorClass({ formats });
        });

        onMounted(async () => {
            const constraints = {
                video: {
                    facingMode: { ideal: this.props.facingMode },
                    width:  { ideal: 1280 },
                    height: { ideal: 720 },
                },
                audio: false,
            };

            try {
                this.stream = await browser.navigator.mediaDevices.getUserMedia(constraints);
            } catch (err) {
                const errorMap = {
                    NotFoundError:   _t("No camera device can be found."),
                    NotAllowedError: _t("Camera permission denied. Please allow camera access."),
                };
                this.props.onError(errorMap[err.name] || err.message);
                return;
            }

            // Guard: component may have been destroyed while awaiting getUserMedia.
            if (status(this) === "destroyed") {
                return;
            }

            this.videoRef.el.srcObject = this.stream;

            const ready = await this._waitForVideo();
            if (!ready) {
                return;
            }

            // Compute zoom ratio for bounding-box adjustments (same as BarcodeDialog).
            const { height, width } = getComputedStyle(this.videoRef.el);
            const tracks = this.stream.getVideoTracks();
            if (tracks.length) {
                const settings = tracks[0].getSettings();
                this.zoomRatio = Math.min(
                    parseFloat(width)  / (settings.width  || 1),
                    parseFloat(height) / (settings.height || 1),
                );
            }

            this.interval = setInterval(
                () => this._detectCode(),
                KioskCamera.DETECT_INTERVAL_MS,
            );
        });

        onWillUnmount(() => {
            this._stopCamera();
        });
    }

    // ── Private helpers ──────────────────────────────────────────────────────

    /** Resolves true when the video element has enough data, false if destroyed. */
    async _waitForVideo() {
        while (!isVideoElementReady(this.videoRef.el)) {
            await delay(10);
            if (status(this) === "destroyed") {
                return false;
            }
        }
        this.state.isReady = true;
        return true;
    }

    /** Stop stream tracks and clear the detection interval. */
    _stopCamera() {
        clearInterval(this.interval);
        this.interval = null;
        if (this.stream) {
            this.stream.getTracks().forEach((t) => t.stop());
            this.stream = null;
        }
    }

    /** Adjust bounding-box coordinates by the zoom ratio. */
    _scaleBox(box, divide = false) {
        const out = {};
        for (const key of Object.keys(box)) {
            out[key] = divide ? box[key] / this.zoomRatio : box[key] * this.zoomRatio;
        }
        return out;
    }

    /** Called on each interval tick; delegates result/error to parent. */
    async _detectCode() {
        if (!this.detector || !this.videoRef.el) {
            return;
        }
        try {
            const codes = await this.detector.detect(this.videoRef.el);
            for (const code of codes) {
                // If a crop overlay is active, skip codes outside its bounds.
                if (this.overlayInfo.x && this.overlayInfo.y) {
                    const box = this._scaleBox(code.boundingBox);
                    if (
                        box.x < this.overlayInfo.x ||
                        box.x + box.width  > this.overlayInfo.x + this.overlayInfo.width ||
                        box.y < this.overlayInfo.y ||
                        box.y + box.height > this.overlayInfo.y + this.overlayInfo.height
                    ) {
                        continue;
                    }
                }
                this.props.onBarcodeScanned(code.rawValue);
                break;  // Only fire once per frame.
            }
        } catch {
            // Ignore transient detection errors (blurry frame, etc.).
        }
    }

    /** Callback from CropOverlay when the crop region changes. */
    onResize(overlayInfo) {
        this.overlayInfo = overlayInfo;
    }
}


// ---------------------------------------------------------------------------
// LunchKiosk
//
// Main kiosk component. Renders either KioskCamera (front/back mode) or a
// scanner placeholder (USB/BT mode). Handles RPC calls and user feedback.
// The visible text input is removed; a hidden input still captures USB
// scanner keystrokes at all times.
// ---------------------------------------------------------------------------
export class LunchKiosk extends Component {
    static template = "xe_lunch_kiosk.LunchKiosk";
    static components = { MainComponentsContainer, KioskCamera };
    static props = {
        barcodeSource: { type: String },  // 'scanner' | 'front' | 'back'
    };

    // Tuneable timing constants (ms)
    static SCAN_DEBOUNCE_DELAY    = 300;
    static MIN_BARCODE_LENGTH     = 4;
    static FEEDBACK_DISPLAY_DELAY = 4000;
    static FOCUS_INTERVAL         = 3000;
    static DATE_REFRESH_INTERVAL  = 60_000;
    static SCAN_COOLDOWN          = 3_000;  // prevent duplicate camera scans

    setup() {
        this.rpc      = useService("rpc");
        this.inputRef = useRef("barcodeInput");

        this.state = useState({
            feedback: {
                visible:    false,
                type:       "",
                title:      "",
                subMessage: "",
            },
            currentDate:   "",
            cameraError:   "",   // non-empty → show error message instead of camera
        });

        this._debounceTimer  = null;
        this._feedbackTimer  = null;
        this._focusInterval  = null;
        this._dateInterval   = null;
        this._onInteraction  = null;
        this._lastCameraCode = "";
        this._lastCameraTime = 0;

        onMounted(() => {
            this._updateDate();
            this._dateInterval = setInterval(
                () => this._updateDate(),
                LunchKiosk.DATE_REFRESH_INTERVAL,
            );

            // Keep the hidden input focused so USB/BT scanners always work.
            this._onInteraction = () => this._focusInput();
            document.addEventListener("click",   this._onInteraction);
            document.addEventListener("keydown", this._onInteraction);

            this._focusInterval = setInterval(
                () => this._focusInput(),
                LunchKiosk.FOCUS_INTERVAL,
            );

            this._focusInput();
        });

        onWillUnmount(() => {
            clearTimeout(this._debounceTimer);
            clearTimeout(this._feedbackTimer);
            clearInterval(this._focusInterval);
            clearInterval(this._dateInterval);
            document.removeEventListener("click",   this._onInteraction);
            document.removeEventListener("keydown", this._onInteraction);
        });
    }

    // ── Computed properties ──────────────────────────────────────────────────

    /** True when the camera view should be rendered. */
    get showCamera() {
        return this.props.barcodeSource !== "scanner";
    }

    /** facingMode forwarded to KioskCamera. */
    get facingMode() {
        return this.props.barcodeSource === "front" ? "user" : "environment";
    }

    // ── Date ─────────────────────────────────────────────────────────────────

    _updateDate() {
        this.state.currentDate = new Date().toLocaleDateString("es-MX", {
            weekday: "long",
            year:    "numeric",
            month:   "long",
            day:     "numeric",
        });
    }

    // ── Focus management (USB/BT scanners) ───────────────────────────────────

    _focusInput() {
        this.inputRef.el?.focus();
    }

    // ── Camera callbacks ─────────────────────────────────────────────────────

    /**
     * Called by KioskCamera each time a barcode is decoded from the video stream.
     * A cooldown prevents the same code from firing twice in quick succession.
     */
    onCameraBarcode(barcode) {
        const now = Date.now();
        if (
            barcode === this._lastCameraCode &&
            now - this._lastCameraTime < LunchKiosk.SCAN_COOLDOWN
        ) {
            return;
        }
        this._lastCameraCode = barcode;
        this._lastCameraTime = now;
        this._processBarcode(barcode);
    }

    /** Called by KioskCamera when the camera stream cannot be started. */
    onCameraError(message) {
        this.state.cameraError = message;
    }

    // ── USB / BT scanner input ───────────────────────────────────────────────

    /** Keydown handler on the hidden input element. */
    onInputKeydown(ev) {
        clearTimeout(this._debounceTimer);

        if (ev.key === "Enter") {
            const barcode = ev.target.value;
            ev.target.value = "";
            this._processBarcode(barcode);
            return;
        }

        // Some USB scanners don't send Enter — flush on short idle.
        this._debounceTimer = setTimeout(() => {
            const barcode = ev.target.value;
            if (barcode.length >= LunchKiosk.MIN_BARCODE_LENGTH) {
                ev.target.value = "";
                this._processBarcode(barcode);
            }
        }, LunchKiosk.SCAN_DEBOUNCE_DELAY);
    }

    // ── Barcode processing ───────────────────────────────────────────────────

    async _processBarcode(barcode) {
        if (!barcode?.trim()) {
            return;
        }

        try {
            const result = await this.rpc("/lunch/kiosk/scan", {
                barcode: barcode.trim(),
            });

            if (result.status === "ok") {
                this._showFeedback(
                    "ok",
                    `✅ ${result.name}`,
                    _t("Successful registration"),
                );
            } else if (result.status === "already_registered") {
                this._showFeedback(
                    "warning",
                    `⚠️ ${result.name}`,
                    _t("Already registered today"),
                );
            } else {
                this._showFeedback(
                    "error",
                    _t("❌ Not found"),
                    _t("Barcode not recognized"),
                );
            }
        } catch {
            this._showFeedback(
                "error",
                _t("❌ Connection error"),
                _t("Check your network and try again"),
            );
        }
    }

    // ── Feedback ─────────────────────────────────────────────────────────────

    _showFeedback(type, title, subMessage) {
        clearTimeout(this._feedbackTimer);

        Object.assign(this.state.feedback, { visible: true, type, title, subMessage });

        this._feedbackTimer = setTimeout(() => {
            this.state.feedback.visible = false;
        }, LunchKiosk.FEEDBACK_DISPLAY_DELAY);
    }
}


// ---------------------------------------------------------------------------
// Bootstrap
// Reads the barcodeSource injected by the Qweb template and mounts the app.
// ---------------------------------------------------------------------------
export async function createLunchKiosk(doc) {
    await whenReady();

    const target = doc.querySelector(".o_lunch_kiosk_app");
    if (!target) {
        return;
    }

    // barcodeSource is written to document.kiosk by kiosk_template.xml.
    const barcodeSource = doc.kiosk?.barcodeSource ?? "back";

    const env = makeEnv();
    await startServices(env);

    const app = new App(LunchKiosk, {
        templates,
        env,
        props:                  { barcodeSource },
        dev:                    env.debug,
        translateFn:            _t,
        translatableAttributes: ["data-tooltip"],
    });

    return app.mount(target);
}

export default { LunchKiosk, KioskCamera, createLunchKiosk };

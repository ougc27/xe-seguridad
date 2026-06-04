/** @odoo-module **/
/**
 * Sale Projection Board — OWL Component (Odoo 17)
 * ================================================
 * Client action: renders an interactive weekly matrix
 *   Rows    → SKUs (product.product)
 *   Columns → ISO weeks starting from W-1 (previous week)
 *
 * Sub-columns per week cell:
 *   [P] Projected  — editable input, saved on blur / Enter
 *   [S] Scheduled  — read-only, from stock.picking.scheduled_date
 *   [X] Executed   — read-only, from stock.picking remission_date / date_done
 *
 * Partner is selected via a Many2one widget at the top.
 * The board auto-refreshes when the partner changes.
 */

import { Component, useState, onMounted, useRef } from "@odoo/owl";
import { registry } from "@web/core/registry";
import { useService } from "@web/core/utils/hooks";
import { Many2OneField } from "@web/views/fields/many2one/many2one_field";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/**
 * Format a float as a locale-aware number string.
 * Returns '' for zero so the cell looks clean.
 */
function fmtQty(val) {
    if (!val || val === 0) return '';
    return val.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

/**
 * Format a float as currency. Returns '' for zero.
 */
function fmtAmt(val) {
    if (!val || val === 0) return '';
    return val.toLocaleString(undefined, {
        minimumFractionDigits: 0,
        maximumFractionDigits: 0,
    });
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

class SaleProjectionBoard extends Component {
    static template = "sale_projection.Board";

    setup() {
        this.orm = useService("orm");
        this.notification = useService("notification");
        this.action = useService("action");

        this.state = useState({
            partnerId: null,
            partnerName: "",
            loading: false,
            boardData: null,      // {weeks, rows, partner_id, partner_name}
            numWeeks: 8,
            pendingSaves: {},     // { lineKey: true } → shows spinner per cell
            errorMsg: null,
        });

        // Load list of obras for the partner selector
        this.partnerSelectRef = useRef("partnerSelect");
    }

    // -----------------------------------------------------------------------
    // Partner selection
    // -----------------------------------------------------------------------

    async onPartnerChange(ev) {
        const id = parseInt(ev.target.value, 10);
        if (!id) {
            this.state.partnerId = null;
            this.state.boardData = null;
            return;
        }
        this.state.partnerId = id;
        this.state.partnerName = ev.target.options[ev.target.selectedIndex].text;
        await this.loadBoard();
    }

    // -----------------------------------------------------------------------
    // Load board data from Python RPC
    // -----------------------------------------------------------------------

    async loadBoard() {
        if (!this.state.partnerId) return;
        this.state.loading = true;
        this.state.errorMsg = null;
        try {
            const data = await this.orm.call(
                "sale.projection.line",
                "get_projection_board_data",
                [this.state.partnerId, this.state.numWeeks],
            );
            this.state.boardData = data;
        } catch (err) {
            this.state.errorMsg = `Error loading board: ${err.message || err}`;
        } finally {
            this.state.loading = false;
        }
    }

    // -----------------------------------------------------------------------
    // Cell editing: save projected quantity
    // -----------------------------------------------------------------------

    /**
     * Called on input blur or Enter key in a projected qty cell.
     */
    async onProjectedQtyChange(ev, productId, weekStart, currentLineId) {
        const rawVal = parseFloat(ev.target.value) || 0;
        const lineKey = `${productId}_${weekStart}`;

        // Optimistic UI: update local state immediately
        this._updateLocalQtyProjected(productId, weekStart, rawVal);

        this.state.pendingSaves[lineKey] = true;
        try {
            const result = await this.orm.call(
                "sale.projection.line",
                "save_projected_qty",
                [this.state.partnerId, productId, weekStart, rawVal],
            );
            // Update amount and line_id from server response
            this._updateLocalLineId(productId, weekStart, result.line_id, result.amount, result.unit_price);
        } catch (err) {
            this.notification.add(
                `Could not save quantity: ${err.message || err}`,
                { type: "danger", title: "Save Error" }
            );
        } finally {
            delete this.state.pendingSaves[lineKey];
        }
    }

    onInputKeydown(ev, productId, weekStart, currentLineId) {
        if (ev.key === "Enter") {
            ev.target.blur();
        }
        if (ev.key === "Escape") {
            ev.target.blur();
        }
    }

    // -----------------------------------------------------------------------
    // Local state helpers
    // -----------------------------------------------------------------------

    _updateLocalQtyProjected(productId, weekStart, val) {
        if (!this.state.boardData) return;
        for (const row of this.state.boardData.rows) {
            if (row.product_id === productId) {
                for (const cell of row.cells) {
                    if (cell.week_start === weekStart) {
                        cell.qty_projected = val;
                        return;
                    }
                }
            }
        }
    }

    _updateLocalLineId(productId, weekStart, lineId, amount, unitPrice) {
        if (!this.state.boardData) return;
        for (const row of this.state.boardData.rows) {
            if (row.product_id === productId) {
                for (const cell of row.cells) {
                    if (cell.week_start === weekStart) {
                        cell.line_id = lineId;
                        cell.amount = amount;
                        cell.unit_price = unitPrice;
                        return;
                    }
                }
            }
        }
    }

    // -----------------------------------------------------------------------
    // Refresh all quantities (manual trigger)
    // -----------------------------------------------------------------------

    async onRefreshAll() {
        if (!this.state.partnerId) return;
        this.state.loading = true;
        try {
            // Find all line IDs in current board
            const lineIds = [];
            for (const row of (this.state.boardData?.rows || [])) {
                for (const cell of row.cells) {
                    if (cell.line_id) lineIds.push(cell.line_id);
                }
            }
            if (lineIds.length) {
                await this.orm.call(
                    "sale.projection.line",
                    "action_refresh_quantities",
                    [lineIds],
                );
            }
            await this.loadBoard();
            this.notification.add("Quantities refreshed from inventory.", {
                type: "success",
                title: "Refreshed",
            });
        } catch (err) {
            this.notification.add(`Refresh error: ${err.message || err}`, {
                type: "danger",
            });
        } finally {
            this.state.loading = false;
        }
    }

    // -----------------------------------------------------------------------
    // Navigation to list view
    // -----------------------------------------------------------------------

    onOpenLines() {
        if (!this.state.partnerId) return;
        this.action.doAction({
            type: "ir.actions.act_window",
            name: `Projection Lines — ${this.state.partnerName}`,
            res_model: "sale.projection.line",
            view_mode: "tree,form,pivot",
            domain: [["partner_id", "=", this.state.partnerId]],
            context: { search_default_partner_id: this.state.partnerId },
        });
    }

    // -----------------------------------------------------------------------
    // Computed column totals
    // -----------------------------------------------------------------------

    getColumnTotals() {
        if (!this.state.boardData) return [];
        const totals = {};
        for (const week of this.state.boardData.weeks) {
            totals[week.week_start] = { proj: 0, sched: 0, exec: 0, amt: 0 };
        }
        for (const row of this.state.boardData.rows) {
            for (const cell of row.cells) {
                const t = totals[cell.week_start];
                if (t) {
                    t.proj += cell.qty_projected || 0;
                    t.sched += cell.qty_scheduled || 0;
                    t.exec += cell.qty_executed || 0;
                    t.amt += cell.amount || 0;
                }
            }
        }
        return this.state.boardData.weeks.map((w) => ({
            week_start: w.week_start,
            ...totals[w.week_start],
        }));
    }

    // -----------------------------------------------------------------------
    // Template helpers exposed to OWL template
    // -----------------------------------------------------------------------

    fmtQty(val) { return fmtQty(val); }
    fmtAmt(val) { return fmtAmt(val); }

    isSaving(productId, weekStart) {
        return !!this.state.pendingSaves[`${productId}_${weekStart}`];
    }
}

// ---------------------------------------------------------------------------
// OWL inline template (avoids needing a separate XML template file for assets)
// ---------------------------------------------------------------------------

SaleProjectionBoard.template = /* xml */ `
<div class="o_sale_projection_board">

    <!-- =========================================================
         HEADER
         ========================================================= -->
    <div class="o_projection_board_header">
        <h2>
            <i class="fa fa-table me-2" aria-hidden="true"/>
            Projection Board
            <t t-if="state.partnerName">
                — <span class="text-primary" t-esc="state.partnerName"/>
            </t>
        </h2>

        <!-- Partner selector -->
        <div class="o_partner_selector">
            <PartnerDropdown
                onchange.bind="onPartnerChange"
                selected_id="state.partnerId"
            />
        </div>

        <!-- Action buttons -->
        <button class="btn btn-outline-secondary btn-sm o_refresh_btn"
                t-on-click="onRefreshAll"
                t-att-disabled="!state.partnerId or state.loading">
            <i class="fa fa-refresh" t-att-class="state.loading ? 'fa-spin' : ''"/>
            Refresh from Stock
        </button>

        <button class="btn btn-outline-primary btn-sm"
                t-on-click="onOpenLines"
                t-att-disabled="!state.partnerId">
            <i class="fa fa-list-ul"/>
            Open Lines
        </button>
    </div>

    <!-- =========================================================
         ERROR MESSAGE
         ========================================================= -->
    <t t-if="state.errorMsg">
        <div class="alert alert-danger" t-esc="state.errorMsg"/>
    </t>

    <!-- =========================================================
         NO PARTNER SELECTED
         ========================================================= -->
    <t t-if="!state.partnerId and !state.loading">
        <div class="o_projection_empty">
            <div class="empty_icon"><i class="fa fa-building-o"/></div>
            <h4>Select a Partner (Obra) to load the projection board</h4>
            <p class="text-muted">
                Use the dropdown above to choose a construction site.
            </p>
        </div>
    </t>

    <!-- =========================================================
         LOADING
         ========================================================= -->
    <t t-if="state.loading">
        <div class="o_projection_loading">
            <i class="fa fa-spinner fa-spin fa-2x"/>
            <span>Loading projection data…</span>
        </div>
    </t>

    <!-- =========================================================
         MATRIX TABLE
         ========================================================= -->
    <t t-if="state.boardData and !state.loading">

        <t t-if="!state.boardData.rows.length">
            <div class="o_projection_empty">
                <div class="empty_icon"><i class="fa fa-inbox"/></div>
                <h4>No SKUs in portfolio for this partner</h4>
                <p class="text-muted">
                    Go to the partner form → Projection tab → add products to the SKU Portfolio.
                </p>
            </div>
        </t>

        <t t-else="">
            <div class="o_projection_matrix_wrapper">
                <table class="o_projection_matrix">

                    <!-- -----------------------------------------------
                         THEAD: Week headers + sub-labels
                         ----------------------------------------------- -->
                    <thead>
                        <!-- Row 1: week labels -->
                        <tr>
                            <th class="th_sku" rowspan="2">SKU / Product</th>
                            <t t-foreach="state.boardData.weeks" t-as="week" t-key="week.week_start">
                                <th class="th_week"
                                    t-att-class="week.is_current ? 'current_week' : week.is_past ? 'past_week' : ''">
                                    <t t-esc="week.label"/>
                                    <t t-if="week.is_current">
                                        <span class="ms-1 badge bg-info" style="font-size:0.6rem">NOW</span>
                                    </t>
                                </th>
                            </t>
                        </tr>
                        <!-- Row 2: P / S / X sub-labels -->
                        <tr class="sub_header">
                            <th class="th_sku_sub">REF | Name</th>
                            <t t-foreach="state.boardData.weeks" t-as="week" t-key="week.week_start + '_sub'">
                                <th>
                                    <span style="color:#90caf9">P</span>
                                    &amp;nbsp;/&amp;nbsp;
                                    <span style="color:#80deea">S</span>
                                    &amp;nbsp;/&amp;nbsp;
                                    <span style="color:#a5d6a7">X</span>
                                </th>
                            </t>
                        </tr>
                    </thead>

                    <!-- -----------------------------------------------
                         TBODY: one row per SKU
                         ----------------------------------------------- -->
                    <tbody>
                        <t t-foreach="state.boardData.rows" t-as="row" t-key="row.product_id">
                            <tr>
                                <!-- SKU label -->
                                <td class="td_sku">
                                    <span t-if="row.product_ref" class="sku_ref"
                                          t-esc="row.product_ref"/>
                                    <t t-esc="row.product_name"/>
                                </td>

                                <!-- Week cells -->
                                <t t-foreach="row.cells" t-as="cell" t-key="cell.week_start">
                                    <td t-att-class="
                                        (state.boardData.weeks[cell_index].is_current ? 'current_week_col' : '') + ' ' +
                                        (state.boardData.weeks[cell_index].is_past ? 'past_week_col' : '')
                                    ">
                                        <div class="o_projection_cell_group">

                                            <!-- [P] Projected — editable -->
                                            <div class="o_proj_cell">
                                                <span class="cell_label" style="color:#90caf9">P</span>
                                                <t t-if="isSaving(row.product_id, cell.week_start)">
                                                    <i class="fa fa-spinner fa-spin" style="font-size:0.8rem;color:#007bff"/>
                                                </t>
                                                <t t-else="">
                                                    <input
                                                        type="number"
                                                        min="0"
                                                        step="1"
                                                        class="o_proj_qty_input"
                                                        t-att-value="cell.qty_projected or ''"
                                                        t-on-blur="(ev) => this.onProjectedQtyChange(ev, row.product_id, cell.week_start, cell.line_id)"
                                                        t-on-keydown="(ev) => this.onInputKeydown(ev, row.product_id, cell.week_start, cell.line_id)"
                                                        placeholder="0"
                                                    />
                                                </t>
                                            </div>

                                            <!-- [S] Scheduled — read-only -->
                                            <div class="o_proj_cell">
                                                <span class="cell_label" style="color:#80deea">S</span>
                                                <span class="o_qty_display o_qty_sched"
                                                      t-att-class="cell.qty_scheduled ? 'has_value' : ''"
                                                      t-esc="fmtQty(cell.qty_scheduled) or '—'"/>
                                            </div>

                                            <!-- [X] Executed — read-only -->
                                            <div class="o_proj_cell">
                                                <span class="cell_label" style="color:#a5d6a7">X</span>
                                                <span class="o_qty_display o_qty_exec"
                                                      t-att-class="cell.qty_executed ? 'has_value' : ''"
                                                      t-esc="fmtQty(cell.qty_executed) or '—'"/>
                                            </div>

                                        </div>

                                        <!-- Amount (shown below, smaller) -->
                                        <t t-if="cell.amount">
                                            <span class="o_proj_amount"
                                                  t-esc="'$' + fmtAmt(cell.amount)"/>
                                        </t>
                                    </td>
                                </t>
                            </tr>
                        </t>
                    </tbody>

                    <!-- -----------------------------------------------
                         TFOOT: column totals
                         ----------------------------------------------- -->
                    <tfoot>
                        <tr>
                            <td class="td_sku">TOTALS</td>
                            <t t-foreach="getColumnTotals()" t-as="tot" t-key="tot.week_start">
                                <td>
                                    <div class="o_projection_cell_group">
                                        <div class="o_proj_cell">
                                            <span class="cell_label" style="color:#90caf9">P</span>
                                            <strong t-esc="fmtQty(tot.proj) or '—'"/>
                                        </div>
                                        <div class="o_proj_cell">
                                            <span class="cell_label" style="color:#80deea">S</span>
                                            <strong t-esc="fmtQty(tot.sched) or '—'"/>
                                        </div>
                                        <div class="o_proj_cell">
                                            <span class="cell_label" style="color:#a5d6a7">X</span>
                                            <strong t-esc="fmtQty(tot.exec) or '—'"/>
                                        </div>
                                    </div>
                                    <t t-if="tot.amt">
                                        <span class="o_proj_amount">
                                            $<t t-esc="fmtAmt(tot.amt)"/>
                                        </span>
                                    </t>
                                </td>
                            </t>
                        </tr>
                    </tfoot>

                </table>
            </div>

            <!-- Legend -->
            <div class="o_projection_legend mt-3">
                <div class="legend_item">
                    <div class="legend_dot" style="background:#90caf9"/>
                    <span><b>P</b> = Projected (manual input)</span>
                </div>
                <div class="legend_item">
                    <div class="legend_dot" style="background:#80deea"/>
                    <span><b>S</b> = Scheduled (from stock.picking)</span>
                </div>
                <div class="legend_item">
                    <div class="legend_dot" style="background:#a5d6a7"/>
                    <span><b>X</b> = Executed (delivered)</span>
                </div>
                <div class="legend_item text-muted">
                    Amounts shown = unit price × projected qty
                </div>
            </div>
        </t>
    </t>

</div>
`;

// ---------------------------------------------------------------------------
// Partner dropdown sub-component
// ---------------------------------------------------------------------------

class PartnerDropdown extends Component {
    static template = /* xml */ `
    <select class="form-select form-select-sm"
            t-on-change="props.onchange"
            t-att-value="props.selected_id or ''">
        <option value="">— Select Obra / Partner —</option>
        <t t-foreach="state.obras" t-as="obra" t-key="obra.id">
            <option t-att-value="obra.id"
                    t-att-selected="obra.id === props.selected_id"
                    t-esc="obra.display_name"/>
        </t>
    </select>
    `;

    static props = {
        onchange: Function,
        selected_id: { type: Number, optional: true },
    };

    setup() {
        this.orm = useService("orm");
        this.state = useState({ obras: [] });
        onMounted(() => this.loadObras());
    }

    async loadObras() {
        const results = await this.orm.searchRead(
            "res.partner",
            [["is_obra", "=", true], ["active", "=", true]],
            ["id", "display_name"],
            { order: "display_name asc", limit: 500 }
        );
        this.state.obras = results;
    }
}

SaleProjectionBoard.components = { PartnerDropdown };

// ---------------------------------------------------------------------------
// Register as a client action
// ---------------------------------------------------------------------------

registry.category("actions").add("sale_projection.board", SaleProjectionBoard);

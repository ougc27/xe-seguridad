/** @odoo-module */

import { Domain } from "@web/core/domain";
import { patch } from "@web/core/utils/patch";
import { InnerGroup } from "@web/views/form/form_group/form_group";
import { Field } from "@web/views/fields/field";
import { session } from "@web/session";
import { evalDomain } from "@web/core/domain";
import { evaluateExpr, evaluateBooleanExpr } from "@web/core/py_js/py";
import { getFieldContext } from "@web/model/relational_model/utils";


export function getFieldDomain(record, fieldName) {
    const { domain } = record.fields[fieldName];
    return typeof domain === "string"
        ? new Domain(evaluateExpr(domain, record.evalContext)).toList()
        : domain || [];
}

patch(InnerGroup.prototype, {
	 //    Overrider method for make field readonly
	getRows() {
        const maxCols = this.props.maxCols;

        const rows = [];
        let currentRow = [];
        let reservedSpace = 0;

        // Dispatch items across table rows
        const items = this.getItems();
        while (items.length) {
            const [slotName, slot] = items.shift();
            if (!slot.isVisible) {
                continue;
            }

            const { newline, itemSpan } = slot;
            if (newline) {
                rows.push(currentRow);
                currentRow = [];
                reservedSpace = 0;
            }

            const fullItemSpan = itemSpan || 1;

            if (fullItemSpan + reservedSpace > maxCols) {
                rows.push(currentRow);
                currentRow = [];
                reservedSpace = 0;
            }

            const isVisible = !("isVisible" in slot) || slot.isVisible;
            currentRow.push({ ...slot, name: slotName, itemSpan, isVisible });
            reservedSpace += itemSpan || 1;

            // Allows to remove the line if the content is not visible instead of leaving an empty line.
            currentRow.isVisible = currentRow.isVisible || isVisible;
        }
        rows.push(currentRow);

        // Compute the relative size of non-label cells
        // The aim is for cells containing business data to occupy as much space as possible
        rows.forEach((row) => {
            let labelCount = 0;
            const dataCells = [];
            for (const c of row) {
                if (c.props && c.props.fieldInfo.attrs.groups_readonly) {
                    c.props.fieldInfo.readonly = true
                    for (const group of c.props.fieldInfo.attrs.groups_readonly.split(",")) {
                        if (session.user_has_groups_readonly.includes(group.trim())){
                            c.props.fieldInfo.readonly = c.props.fieldInfo.attrs.readonly
                            break;
                        }
                    }
                }

                if (c.subType === "label") {
                    labelCount++;
                } else if (c.subType === "item_component") {
                    labelCount++;
                    dataCells.push(c);
                } else {
                    dataCells.push(c);
                }
            }

            const sizeOfDataCell = 100 / (maxCols - labelCount);
            dataCells.forEach((c) => {
                const itemSpan = c.subType === "item_component" ? c.itemSpan - 1 : c.itemSpan;
                c.width = (itemSpan || 1) * sizeOfDataCell;
            });
        });
        return rows;
    }
});


patch(Field.prototype, {
	 //    Overrider method for make field readonly
	get fieldComponentProps() {
        const record = this.props.record;
        let readonly = this.props.readonly || false;

        let propsFromNode = {};
        if (this.props.fieldInfo) {
            let fieldInfo = this.props.fieldInfo;

            if (fieldInfo.attrs.groups_readonly) {
	            fieldInfo.readonly = true
	            for (const group of fieldInfo.attrs.groups_readonly.split(",")) {
	                if (session.user_has_groups_readonly.includes(group.trim())){
	                    fieldInfo.readonly = fieldInfo.attrs.readonly
	                    break;
	                }
	            }
	        }

            readonly =
                readonly ||
                evaluateBooleanExpr(fieldInfo.readonly, record.evalContextWithVirtualIds);

            if (this.field.extractProps) {
                if (this.props.attrs) {
                    fieldInfo = {
                        ...fieldInfo,
                        attrs: { ...fieldInfo.attrs, ...this.props.attrs },
                    };
                }

                const dynamicInfo = {
                    get context() {
                        return getFieldContext(record, fieldInfo.name, fieldInfo.context);
                    },
                    domain() {
                        if (fieldInfo.domain) {
                            return new Domain(
                                evaluateExpr(fieldInfo.domain, record.evalContext)
                            ).toList();
                        }
                        return getFieldDomain(record, fieldInfo.name);
                    },
                    required: evaluateBooleanExpr(
                        fieldInfo.required,
                        record.evalContextWithVirtualIds
                    ),
                    readonly: readonly,
                };
                propsFromNode = this.field.extractProps(fieldInfo, dynamicInfo);
            }
        }

        const props = { ...this.props };
        delete props.style;
        delete props.class;
        delete props.showTooltip;
        delete props.fieldInfo;
        delete props.attrs;
        delete props.type;
        delete props.readonly;

        return {
            readonly: readonly || !record.isInEdition || false,
            ...propsFromNode,
            ...props,
        };
    }
});

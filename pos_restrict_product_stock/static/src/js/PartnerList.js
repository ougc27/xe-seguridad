/** @odoo-module **/
import { patch } from "@web/core/utils/patch";
import { PartnerListScreen } from "@point_of_sale/app/screens/partner_list/partner_list";


patch(PartnerListScreen.prototype, {
    async saveChanges(processedChanges) {
        if (this.pos.config.crm_team_id) {
            processedChanges.team_id = this.pos.config.crm_team_id[0];
        }
        if (this.pos.user) {
            processedChanges.user_id = this.pos.user.id;
        }
        const partnerId = await this.orm.call("res.partner", "create_from_ui", [processedChanges]);
        await this.pos._loadPartners([partnerId]);
        this.state.selectedPartner = this.pos.db.get_partner_by_id(partnerId);
        this.confirm();
    }
});

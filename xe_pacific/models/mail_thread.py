from odoo import models, api, exceptions, _


class MailThread(models.AbstractModel):
    _inherit = 'mail.thread'

    # ------------------------------------------------------------
    # MAIL.MESSAGE HELPERS
    # ------------------------------------------------------------

    def _message_compute_author(self, author_id=None, email_from=None, raise_on_email=True):
        """ Tool method computing author information for messages. Purpose is
        to ensure maximum coherence between author / current user / email_from
        when sending emails.

        :param raise_on_email: if email_from is not found, raise an UserError

        :return tuple: res.partner ID (may be False or None), email_from
        """
        if author_id is None:
            if email_from:
                author = self._mail_find_partner_from_emails([email_from])[0]
            else:
                author = self.env.user.partner_id
                email_from = author.email_formatted
            author_id = author.id

        if email_from is None:
            if author_id:
                author = self.env['res.partner'].browse(author_id)
                email_from = author.email_formatted

        # superuser mode without author email -> probably public user; anyway we don't want to crash
        if not email_from and raise_on_email and not self.env.su:
                email_from = 'sistemas@xeseguridad.com'
                #raise exceptions.UserError(_("Unable to send message, please configure the sender's email address."))

        return author_id, email_from


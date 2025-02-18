# -*- coding: utf-8 -*-

from email import header
import json
import base64
import logging
import requests
from werkzeug import urls

from odoo import _, fields, models, service, api
from odoo.exceptions import ValidationError
from odoo.http import request


_logger = logging.getLogger(__name__)


class PaymentProviderMollie(models.Model):
    _inherit = 'payment.provider'

    # removed required_if_provider becasue we do not want to add production key during testing
    mollie_api_key = fields.Char(string="Mollie API Key", required_if_provider=False, help="The Test or Live API Key depending on the configuration of the provider", groups="base.group_system")
    mollie_api_key_test = fields.Char(string="Test API key", groups="base.group_user")
    mollie_profile_id = fields.Char("Mollie Profile ID", groups="base.group_user")
    mollie_methods_ids = fields.One2many('mollie.payment.method', 'provider_id', string='Mollie Payment Methods')

    mollie_use_components = fields.Boolean(string='Mollie Components', default=True)
    mollie_show_save_card = fields.Boolean(string='Single-Click payments')

    # ----------------
    # PAYMENT FEATURES
    # ----------------

    def _compute_feature_support_fields(self):
        """ Override of `payment` to enable additional features. """
        super()._compute_feature_support_fields()
        self.filtered(lambda p: p.code == 'mollie').update({
            'support_refund': 'partial',
            'support_fees': True,
            'support_manual_capture': True
        })

    # --------------
    # ACTION METHODS
    # --------------

    def action_mollie_sync_methods(self):
        """ This method will sync mollie methods and translations via API """
        self.ensure_one()
        methods = self._api_mollie_get_active_payment_methods(all_methods=True)
        if methods:
            self._sync_mollie_methods(methods)
            self._create_method_translations()

    # ----------------
    # Fees METHODs
    # ----------------

    def _compute_mollie_method_fees(self, fees_by_provider, invoice=None, order=None, amount=None, currency=None, partner_id=None):
        """ This method adds fees for mollie's methods in Odoo's fees_by_provider.

        :param dict fees_by_provider: fees and payment methods mepping
        :param recordset order: order recordset to fetch amount currency or partner
        :param float amount: total amount to pay
        :param recordset currency: The currency of the transaction, as a `res.currency` record
        :param recordset country: The customer country, as a `res.country` record
        :return: fees map for the mollie method and payment provider (here values are fees)
        :rtype: dict
        """
        mollie_providers = self.filtered(lambda acq: acq.code == 'mollie')

        if not mollie_providers or not (order or (amount and currency and partner_id)):
            return fees_by_provider

        country_id = False
        if order:
            amount = order.amount_total
            currency = order.currency_id
            country_id = order.partner_id.country_id
        elif partner_id:
            country_id = self.sudo().env['res.partner'].browse(partner_id).country_id

        for mollie_provider in mollie_providers:
            global_fees = mollie_provider._compute_fees(amount, currency, country_id)
            for mollie_method in mollie_provider.mollie_methods_ids:
                fees_key = (mollie_provider, mollie_method)
                fees_by_provider[fees_key] = global_fees

                if mollie_method.fees_active:
                    fees_by_provider[fees_key] = mollie_method._compute_fees(amount, currency, country_id)

        return fees_by_provider

    # --------------------------------------------------------
    # TO SYNC ACTIVE MOLLIE METHODS, ISSUSER AND TRANSLATIONS
    # --------------------------------------------------------

    def _sync_mollie_methods(self, methods_data):
        """ Create/Update the mollie payment methods based on configuration
        in the mollie.com. This will automatically activate/deactivate methods
        based on your configurateion on the mollie.com
        :param dict methods_data: Mollie's method data received from api
        """

        mollie_methods = self.with_context(active_test=False).mollie_methods_ids

        # Create New methods
        MolliePaymentMethod = self.env['mollie.payment.method']
        methods_to_create = methods_data.keys() - set(mollie_methods.mapped('method_code'))
        for method in methods_to_create:
            method_info = methods_data[method]
            create_vals = {
                'name': method_info['description'],
                'method_code': method_info['id'],
                'provider_id': self.id,
            }

            # Manage issuer for the method
            issuers_data = method_info.get('issuers')
            if issuers_data:
                issuer_ids = self._get_issuers_ids(issuers_data)
                if issuer_ids:
                    create_vals['payment_issuer_ids'] = [(6, 0, issuer_ids)]

            # Manage icons for methods
            icon = self.env['payment.icon'].search([('name', '=', method_info['description'])], limit=1)
            image_url = method_info.get('image', {}).get('size2x')
            if not icon and image_url:
                icon = self.env['payment.icon'].create({
                    'name': method_info['description'],
                    'image': self._mollie_fetch_image_by_url(image_url)
                })
            if icon:
                create_vals['payment_icon_ids'] = [(6, 0, [icon.id])]
            mollie_methods += MolliePaymentMethod.create(create_vals)

        for method_code, method_data in methods_data.items():
            issuers_data = method_data.get('issuers', [])
            mollie_method = mollie_methods.filtered(lambda m: m.method_code == method_code)
            # remove the issuer if it removed from mollie (iban2 removed the issuers support)
            mollie_supported_issuer_codes = [issuer_info['id'] for issuer_info in issuers_data]
            issuers_to_delete = mollie_method.payment_issuer_ids.filtered(lambda issuer: issuer.issuers_code not in mollie_supported_issuer_codes)
            if issuers_to_delete:
                issuers_to_delete.unlink()

        # Activate methods & update method data
        for method in mollie_methods:
            method.active = methods_data.get(method.method_code, {}).get('status') == 'activated'

    def _get_issuers_ids(self, issuers_data):
        """ Create/Update the mollie issuers based on issuers data received from
        mollie api.
        :param list issuers_data: Mollie's issuers data received from api
        :return: list of issuers ids
        :rtype: list
        """
        issuer_ids = []
        for issuer_data in issuers_data:
            MollieIssuer = self.env['mollie.payment.method.issuer']
            issuer = MollieIssuer.search([('issuers_code', '=', issuer_data['id'])], limit=1)
            if not issuer:
                issuer_create_vals = {
                    'name': issuer_data['name'],
                    'issuers_code': issuer_data['id'],
                }
                icon = self.env['payment.icon'].search([('name', '=', issuer_data['name'])], limit=1)
                image_url = issuer_data.get('image', {}).get('size2x')
                if not icon and image_url:
                    icon = self.env['payment.icon'].create({
                        'name': issuer_data['name'],
                        'image': self._mollie_fetch_image_by_url(image_url)
                    })
                issuer_create_vals['payment_icon_ids'] = [(6, 0, [icon.id])]
                issuer = MollieIssuer.create(issuer_create_vals)
            issuer_ids.append(issuer.id)
        return issuer_ids

    def _create_method_translations(self):
        """ This method add translated terms for the method names. These translations
        are provided by mollie locale.
        This is required as the method names are stored in fields. Luckily mollie provides
        translated values so we create the translation terms from mollie.
        Note: We only create the terms if it is not present because user might have enterd
        his own translation values,
        """
        supported_locale = self._mollie_get_supported_locale()
        supported_locale.remove('en_US')  # en_US is default
        active_langs = self.env['res.lang'].search([('code', 'in', supported_locale)])
        mollie_methods = self.mollie_methods_ids

        for lang in active_langs:
            methods_data = self._api_mollie_get_active_payment_methods(all_methods=True, extra_params={'locale': lang.code})
            for method in mollie_methods:
                translated_value = methods_data.get(method.method_code, {}).get('description')
                method.with_context(lang=lang.code).write({
                    'name': translated_value
                })

    # -----------------------------------
    # TO FILTER METHODS ON CHECKOUT FORMS
    # -----------------------------------

    def _mollie_get_supported_methods(self, order, invoice, amount, currency, partner_id):
        """ Mollie provides multiple payment methods in single payment provider.
            Support of these varies based on based amount, currency and billing country.
            So this method will filters the mollie's supported payment method based amount,
            currency and billing country.
            Note: we also filter the methods based on geoip and voucher configurations.
            :param dict order: order record for which this transaction is generated
            :return details of supported methods
            :rtype: dict
        """
        methods = self.mollie_methods_ids.filtered(lambda m: m.active and m.active_on_shop)

        # Prepare extra params to filter methods via mollie API
        has_voucher_line, extra_params = False, {'includeWallets': 'applepay'}
        if not order and request.params.get('order_id'):
            order = self.env['sale.order'].sudo().browse(request.params.get('order_id'))
        if order:
            extra_params['amount'] = {'value': "%.2f" % order.amount_total, 'currency': order.currency_id.name}
            extra_params['resource'] = 'orders'
            has_voucher_line = order.mapped('order_line.product_id.product_tmpl_id')._get_mollie_voucher_category()
            if order.partner_invoice_id.country_id:
                extra_params['billingCountry'] = order.partner_invoice_id.country_id.code

        if invoice and invoice._name == 'account.move':
            extra_params['amount'] = {'value': "%.2f" % invoice.amount_residual, 'currency': invoice.currency_id.name}
            if invoice.partner_id.country_id:
                extra_params['billingCountry'] = invoice.partner_id.country_id.code

        if amount and currency:
            extra_params['amount'] = {'value': "%.2f" % amount, 'currency': currency.name}

        if not extra_params.get('billingCountry') and partner_id:
            partner = self.sudo().env['res.partner'].browse(partner_id).exists()
            if partner and partner.country_id:
                extra_params['billingCountry'] = partner.country_id.code

        if has_voucher_line:
            extra_params['orderLineCategories'] = ','.join(has_voucher_line)
        else:
            methods = methods.filtered(lambda m: m.method_code != 'voucher')

        # Hide based on country
        if request:
            country_code = request.geoip and request.geoip.get('country_code') or False
            if country_code:
                methods = methods.filtered(lambda m: not m.country_ids or country_code in m.country_ids.mapped('code'))

        # Hide methods if mollie does not supports them (checks via api call)
        supported_methods = self.sudo()._api_mollie_get_active_payment_methods(extra_params=extra_params)  # sudo as public user do not have access to keys
        methods = methods.filtered(lambda m: m.method_code in supported_methods.keys())

        mollie_issuers = {}
        for method, method_data in supported_methods.items():
            issuers = method_data.get('issuers')
            if issuers:
                mollie_method = self.env['mollie.payment.method'].search([('method_code', '=', method), ('provider_id', '=', self.id)])
                if mollie_method:
                    issuers_codes = list(map(lambda issuer: issuer['id'], issuers))
                    mollie_issuers[mollie_method[0].id] = mollie_method[0].payment_issuer_ids.filtered(lambda issuer: issuer.active and issuer.issuers_code in issuers_codes).ids  # always use first method, didn't occuer any case to get multiple methods but handle it

        return methods.with_context(mollie_issuers=mollie_issuers)

    # -----------
    # API methods
    # -----------

    def _mollie_make_request(self, endpoint, params=None, data=None, method='POST', silent_errors=False):
        """
        Overriden method to manage 'params' rest of the things works as it is.

        We are not using super as we want diffrent User-Agent for all requests.
        We also want to use separate test api key in test mode.

        Note: self.ensure_one()
        :param str endpoint: The endpoint to be reached by the request
        :param dict params: The querystring of the request
        :param dict data: The payload of the request
        :param str method: The HTTP method of the request
        :return The JSON-formatted content of the response
        :rtype: dict
        :raise: ValidationError if an HTTP error occurs
        """
        self.ensure_one()

        endpoint = f'/v2/{endpoint.strip("/")}'
        url = urls.url_join('https://api.mollie.com/', endpoint)
        params = self._mollie_generate_querystring(params)

        # User agent strings used by mollie to find issues in integration
        odoo_version = service.common.exp_version()['server_version']
        mollie_extended_app_version = self.env.ref('base.module_payment_mollie_official').installed_version
        mollie_api_key = self.mollie_api_key_test if self.state == 'test' else self.mollie_api_key

        headers = {
            "Accept": "application/json",
            "Authorization": f'Bearer {mollie_api_key}',
            "Content-Type": "application/json",
            "User-Agent": f'Odoo/{odoo_version} MollieOdoo/{mollie_extended_app_version}',
        }

        error_msg, result = _("Could not establish the connection to the API."), False
        if data:
            data = json.dumps(data)

        try:
            response = requests.request(method, url, params=params, data=data, headers=headers, timeout=60)
            if response.status_code == 204:
                return True  # returned no content
            result = response.json()
            if response.status_code not in [200, 201]:  # doc reference https://docs.mollie.com/overview/handling-errors
                error_msg = f"Error[{response.status_code}]: {result.get('title')} - {result.get('detail')}"
                _logger.exception("Error from mollie: %s", result)
            response.raise_for_status()
        except requests.exceptions.RequestException:
            if silent_errors:
                return response.json()
            else:
                raise ValidationError("Mollie: " + error_msg)
        return result

    def _api_mollie_get_active_payment_methods(self, extra_params=None, all_methods=None):
        """ Get method data from the mollie. It will return the methods
        that are enabled in the Mollie.
        :param dict extra_params: Optional parameters which are passed to mollie during API call
        :return: details of enabled methods
        :rtype: dict
        """
        result = {}
        extra_params = extra_params or {}
        endpoint = '/methods/all' if all_methods else '/methods'
        params = {'include': 'issuers', **extra_params}

        # get payment api methods
        payemnt_api_methods = self._mollie_make_request(endpoint, params=params, method="GET", silent_errors=True)
        if payemnt_api_methods and payemnt_api_methods.get('count'):
            for method in payemnt_api_methods['_embedded']['methods']:
                result[method['id']] = method
        return result or {}

    def _api_mollie_create_payment_record(self, api_type, payment_data, params=None, silent_errors=False):
        """ Create the payment records on the mollie. It calls payment or order
        API based on 'api_type' param.
        :param str api_type: api is selected based on this parameter
        :param dict payment_data: payment data
        :return: details of created payment record
        :rtype: dict
        """
        endpoint = '/orders' if api_type == 'order' else '/payments'
        return self._mollie_make_request(endpoint, data=payment_data, params=params, method="POST", silent_errors=silent_errors)

    def _api_mollie_get_payment_data(self, transaction_reference, force_payment=False):
        """ Fetch the payment records based `transaction_reference`. It is used
        to varify transaction's state after the payment.
        :param str transaction_reference: transaction reference
        :return: details of payment record
        :rtype: dict
        """
        mollie_data = {}
        if transaction_reference.startswith('ord_'):
            mollie_data = self._mollie_make_request(f'/orders/{transaction_reference}', params={'embed': 'payments'}, method="GET")
        if transaction_reference.startswith('tr_'):    # This is not used
            mollie_data = self._mollie_make_request(f'/payments/{transaction_reference}', method="GET")
        if not force_payment:
            return mollie_data

        if mollie_data['resource'] == 'order':
            payments = mollie_data.get('_embedded', {}).get('payments', [])
            if payments:
                # No need to handle multiple payment for same order as we create new order for each failed transaction
                payment_id = payments[0]['id']
                mollie_data = self._mollie_make_request(f'/payments/{payment_id}', method="GET")
        return mollie_data

    def _api_mollie_create_customer_id(self):
        """ Create the customer id for currunt user inside the mollie.
        :return: customer id
        :rtype: cuatomer_data
        """
        sudo_user = self.env.user.sudo()
        customer_data = {'name': sudo_user.name, 'metadata': {'odoo_user_id': self.env.user.id}}
        if sudo_user.email:
            customer_data['email'] = sudo_user.email
        return self._mollie_make_request('/customers', data=customer_data, method="POST")

    def _api_mollie_refund(self, amount, currency, payment_reference):
        """ Create the customer id for currunt user inside the mollie.
        :param str amount: amount to refund
        :param str currency: refund curruncy
        :param str payment_reference: transaction reference for refund
        :return: details of payment record
        :rtype: dict
        """
        refund_data = {'amount': {'value': "%.2f" % amount, 'currency': currency}}
        data = self._mollie_make_request(f'/payments/{payment_reference}/refunds', data=refund_data, method="POST")
        return data

    def _api_mollie_refund_data(self, payment_reference, refund_reference):
        """ Get data for the refund from mollie.
        :param str refund_reference: refund record reference
        :param str payment_reference: refund payment reference
        :return: details of refund record
        :rtype: dict
        """
        return self._mollie_make_request(f'/payments/{payment_reference}/refunds/{refund_reference}', method="GET")

    def _api_get_customer_data(self, customer_id, silent_errors=False):
        """ Create the customer id for currunt user inside the mollie.
        :param str customer_id: customer_id in mollie
        :rtype: dict
        """
        return self._mollie_make_request(f'/customers/{customer_id}', method="GET", silent_errors=silent_errors)

    # -------------------------
    # Helper methods for mollie
    # -------------------------

    def _mollie_user_locale(self):
        user_lang = self.env.context.get('lang')
        supported_locale = self._mollie_get_supported_locale()
        return user_lang if user_lang in supported_locale else 'en_US'

    def _mollie_get_supported_locale(self):
        return [
            'en_US', 'nl_NL', 'nl_BE', 'fr_FR',
            'fr_BE', 'de_DE', 'de_AT', 'de_CH',
            'es_ES', 'ca_ES', 'pt_PT', 'it_IT',
            'nb_NO', 'sv_SE', 'fi_FI', 'da_DK',
            'is_IS', 'hu_HU', 'pl_PL', 'lv_LV',
            'lt_LT', 'en_GB']

    def _mollie_fetch_image_by_url(self, image_url):
        image_base64 = False
        try:
            image_base64 = base64.b64encode(requests.get(image_url).content)
        except Exception:
            _logger.warning('Can not import mollie image %s', image_url)
        return image_base64

    def _mollie_generate_querystring(self, params):
        """ Mollie uses dictionaries in querystrings with square brackets like this
        https://api.mollie.com/v2/methods?amount[value]=125.91&amount[currency]=EUR
        :param dict params: parameters which needs to be converted in mollie format
        :return: querystring in mollie's format
        :rtype: string
        """
        if not params:
            return None
        parts = []
        for param, value in sorted(params.items()):
            if not isinstance(value, dict):
                parts.append(urls.url_encode({param: value}))
            else:
                # encode dictionary with square brackets
                for key, sub_value in sorted(value.items()):
                    composed = f"{param}[{key}]"
                    parts.append(urls.url_encode({composed: sub_value}))
        if parts:
            return "&".join(parts)

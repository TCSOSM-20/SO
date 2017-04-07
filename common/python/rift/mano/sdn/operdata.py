
#
#   Copyright 2017 RIFT.IO Inc
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#

import asyncio
import rift.tasklets

from gi.repository import(
        RwSdnYang,
        RwDts as rwdts,
        )


class SDNAccountNotFound(Exception):
    pass


class SDNAccountDtsOperdataHandler(object):
    def __init__(self, dts, log, loop, project):
        self._dts = dts
        self._log = log
        self._loop = loop
        self._project = project

        self.sdn_accounts = {}
        self._oper = None
        self._rpc = None

    def add_sdn_account(self, account):
        self.sdn_accounts[account.name] = account
        account.start_validate_credentials(self._loop)

    def delete_sdn_account(self, account_name):
        del self.sdn_accounts[account_name]

    def get_saved_sdn_accounts(self, sdn_account_name):
        ''' Get SDN Account corresponding to passed name, or all saved accounts if name is None'''
        saved_sdn_accounts = []

        if sdn_account_name is None or sdn_account_name == "":
            sdn_accounts = list(self.sdn_accounts.values())
            saved_sdn_accounts.extend(sdn_accounts)
        elif sdn_account_name in self.sdn_accounts:
            account = self.sdn_accounts[sdn_account_name]
            saved_sdn_accounts.append(account)
        else:
            errstr = "SDN account {} does not exist".format(sdn_account_name)
            raise KeyError(errstr)

        return saved_sdn_accounts

    def _register_show_status(self):
        def get_xpath(sdn_name=None):
            return self._project.add_project("D,/rw-sdn:sdn/account{}/connection-status".
                                             format(
                                                 "[name='%s']" % sdn_name
                                                 if sdn_name is not None else ''))

        @asyncio.coroutine
        def on_prepare(xact_info, action, ks_path, msg):
            self._log.debug("Got show SDN connection status request: %s", ks_path.create_string())
            path_entry = RwSdnYang.SDNAccount.schema().keyspec_to_entry(ks_path)
            sdn_account_name = path_entry.key00.name

            try:
                saved_accounts = self.get_saved_sdn_accounts(sdn_account_name)
                for account in saved_accounts:
                    connection_status = account.connection_status
                    self._log.debug("Responding to SDN connection status request: %s", connection_status)
                    xact_info.respond_xpath(
                            rwdts.XactRspCode.MORE,
                            xpath=get_xpath(account.name),
                            msg=account.connection_status,
                            )
            except KeyError as e:
                self._log.warning(str(e))
                xact_info.respond_xpath(rwdts.XactRspCode.NA)
                return

            xact_info.respond_xpath(rwdts.XactRspCode.ACK)

        self._oper = yield from self._dts.register(
                xpath=get_xpath(),
                handler=rift.tasklets.DTS.RegistrationHandler(
                    on_prepare=on_prepare),
                flags=rwdts.Flag.PUBLISHER,
                )

    def _register_validate_rpc(self):
        def get_xpath():
            return "/rw-sdn:update-sdn-status"

        @asyncio.coroutine
        def on_prepare(xact_info, action, ks_path, msg):
            if self._project and not self._project.rpc_check(msg, xact_info=xact_info):
             return

            if not msg.has_field("sdn_account"):
                raise SDNAccountNotFound("SDN account name not provided")

            sdn_account_name = msg.sdn_account
            try:
                account = self.sdn_accounts[sdn_account_name]
            except KeyError:
                raise SDNAccountNotFound("SDN account name %s not found" % sdn_account_name)

            account.start_validate_credentials(self._loop)

            xact_info.respond_xpath(rwdts.XactRspCode.ACK)

        self._rpc = yield from self._dts.register(
            xpath=get_xpath(),
            handler=rift.tasklets.DTS.RegistrationHandler(
                on_prepare=on_prepare
            ),
            flags=rwdts.Flag.PUBLISHER,
        )

    @asyncio.coroutine
    def register(self):
        yield from self._register_show_status()
        yield from self._register_validate_rpc()

    def deregister(self):
        if self._oper:
            self._oper.deregister()
            self._oper = None

        if self._rpc:
            self._rpc.deregister()
            self._rpc = None

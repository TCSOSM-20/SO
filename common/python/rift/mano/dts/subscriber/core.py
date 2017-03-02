"""
#
#   Copyright 2016-2017 RIFT.IO Inc
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

@file core.py
@author Varun Prasad (varun.prasad@riftio.com)
@date 09-Jul-2016

"""

import abc
import collections
import asyncio

from gi.repository import (RwDts as rwdts, ProtobufC)
import rift.tasklets
from rift.mano.utils.project import (
    get_add_delete_update_cfgs,
    )

from ..core import DtsHandler


class SubscriberDtsHandler(DtsHandler):
    """A common class for all subscribers.
    """
    @classmethod
    def from_project(cls, proj, callback=None):
        """Convenience method to build the object from tasklet

        Args:
            proj (rift.mano.utils.project.ManoProject): Project
            callback (None, optional): Callable, which will be invoked on
                    subscriber changes.

        Signature of callback:
            Args:
                msg: The Gi Object msg from DTS
                action(rwdts.QueryAction): Action type
        """
        return cls(proj.log, proj.dts, proj.loop, proj, callback=callback)

    def __init__(self, log, dts, loop, project, callback=None):
        super().__init__(log, dts, loop, project)
        self.callback = callback

    def get_reg_flags(self):
        """Default set of REG flags, can be over-ridden by sub classes.

        Returns:
            Set of rwdts.Flag types.
        """
        return rwdts.Flag.SUBSCRIBER|rwdts.Flag.DELTA_READY|rwdts.Flag.CACHE



class AbstractOpdataSubscriber(SubscriberDtsHandler):
    """Abstract class that simplifies the process of creating subscribers
    for opdata.

    Opdata subscriber can be created in one step by subclassing and implementing
    the MANDATORY get_xpath() method

    """
    @abc.abstractmethod
    def get_xpath(self):
        """
        Returns:
           str: xpath
        """
        pass

    @asyncio.coroutine
    def register(self):
        """Triggers the registration
        """

        if self.reg:
            self._log.warning("RPC already registered for project {}".
                              format(self._project.name))
            return

        xacts = {}

        def on_commit(xact_info):
            xact_id = xact_info.handle.get_xact().id
            if xact_id in xacts:
                msg, action = xacts.pop(xact_id)

                if self.callback:
                    self.callback(msg, action)

            return rwdts.MemberRspCode.ACTION_OK

        @asyncio.coroutine
        def on_prepare(xact_info, action, ks_path, msg):
            try:
                # Defer all actions till the commit state.
                xacts[xact_info.xact.id] = (msg, action)

            except Exception as e:
                self.log.exception(e)

            finally:
                xact_info.respond_xpath(rwdts.XactRspCode.ACK)

        reg_event = asyncio.Event(loop=self.loop)

        @asyncio.coroutine
        def on_ready(_, status):
            reg_event.set()

        handler = rift.tasklets.DTS.RegistrationHandler(
                on_ready=on_ready,
                on_prepare=on_prepare,
                on_commit=on_commit
                )

        self.reg = yield from self.dts.register(
                xpath=self.project.add_project(self.get_xpath()),
                flags=self.get_reg_flags(),
                handler=handler)

        # yield from reg_event.wait()

        assert self.reg is not None


class AbstractConfigSubscriber(SubscriberDtsHandler):
    """Abstract class that simplifies the process of creating subscribers
    for config data.

    Config subscriber can be created in one step by subclassing and implementing
    the MANDATORY get_xpath() method

    """
    KEY = "msgs"

    @abc.abstractmethod
    def get_xpath(self):
        pass

    @abc.abstractmethod
    def key_name(self):
        pass

    @asyncio.coroutine
    def register(self):
        """ Register for VNFD configuration"""

        if self.reg:
            self._log.warning("RPC already registered for project {}".
                              format(self._project.name))
            return

        def on_apply(dts, acg, xact, action, scratch):
            """Apply the  configuration"""
            is_recovery = xact.xact is None and action == rwdts.AppconfAction.INSTALL


            add_cfgs, delete_cfgs, update_cfgs = get_add_delete_update_cfgs(
                    dts_member_reg=self.reg,
                    xact=xact,
                    key_name=self.key_name())

            [self.callback(cfg, rwdts.QueryAction.DELETE)
                    for cfg in delete_cfgs if self.callback]

            [self.callback(cfg, rwdts.QueryAction.CREATE)
                    for cfg in add_cfgs if self.callback]

            [self.callback(cfg, rwdts.QueryAction.UPDATE)
                    for cfg in update_cfgs if self.callback]

        @asyncio.coroutine
        def on_prepare(dts, acg, xact, xact_info, ks_path, msg, scratch):
            """ on prepare callback """
            xact_info.respond_xpath(rwdts.XactRspCode.ACK)

        acg_hdl = rift.tasklets.AppConfGroup.Handler(on_apply=on_apply)
        with self.dts.appconf_group_create(handler=acg_hdl) as acg:
            self.reg = acg.register(
                xpath=self.project.add_project(self.get_xpath()),
                flags=self.get_reg_flags(),
                on_prepare=on_prepare)

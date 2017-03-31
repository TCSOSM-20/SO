"""
# 
#   Copyright 2016 RIFT.IO Inc
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

@file rwpkgmgr.py
@author Varun Prasad (varun.prasad@riftio.com)
@date 18-Sep-2016

"""

import asyncio

import gi
gi.require_version('RwDts', '1.0')
gi.require_version('RwPkgMgmtYang', '1.0')


from gi.repository import (
        RwDts as rwdts,
        RwPkgMgmtYang)
import rift.tasklets
from rift.mano.utils.project import (
    ManoProject,
    ProjectHandler,
)

from . import rpc
from .proxy import filesystem
from . import publisher as pkg_publisher
from . import subscriber 

class PackageManagerProject(ManoProject):

    def __init__(self, name, tasklet, **kw):
        super(PackageManagerProject, self).__init__(tasklet.log, name)
        self.update(tasklet)

        args = [self.log, self.dts, self.loop, self]
        self.job_handler = pkg_publisher.DownloadStatusPublisher(*args)
        # create catalog subscribers 
        self.vnfd_catalog_sub = subscriber.VnfdStatusSubscriber(*args)
        self.nsd_catalog_sub = subscriber.NsdStatusSubscriber(*args)


    @asyncio.coroutine
    def register (self):
        yield from self.vnfd_catalog_sub.register()
        yield from self.nsd_catalog_sub.register()
        yield from self.job_handler.register()

    def deregister (self):
        yield from self.job_handler.deregister()
        yield from self.vnfd_catalog_sub.deregister()
        yield from self.nsd_catalog_sub.deregister()


class PackageManagerTasklet(rift.tasklets.Tasklet):
    def __init__(self, *args, **kwargs):
        try:
            super().__init__(*args, **kwargs)
            self.rwlog.set_category("rw-mano-log")
            self.endpoint_rpc = None
            self.schema_rpc = None

            self._project_handler = None
            self.projects = {}

        except Exception as e:
            self.log.exception(e)

    def start(self):
        super().start()

        self.log.debug("Registering with dts")

        self.dts = rift.tasklets.DTS(
                self.tasklet_info,
                RwPkgMgmtYang.get_schema(),
                self.loop,
                self.on_dts_state_change
                )

        proxy = filesystem.FileSystemProxy(self.loop, self.log)

        args = [self.log, self.dts, self.loop]
        args.append(proxy)
        self.endpoint_rpc = rpc.EndpointDiscoveryRpcHandler(*args)
        self.schema_rpc = rpc.SchemaRpcHandler(*args)
        self.delete_rpc = rpc.PackageDeleteOperationsRpcHandler(*args)

        args.append(self)
        self.pkg_op = rpc.PackageOperationsRpcHandler(*args)

    def stop(self):
        try:
            self.dts.deinit()
        except Exception as e:
            self.log.exception(e)

    @asyncio.coroutine
    def init(self):
        yield from self.endpoint_rpc.register()
        yield from self.schema_rpc.register()
        yield from self.pkg_op.register()
        yield from self.delete_rpc.register()

        self.log.debug("creating project handler")
        self.project_handler = ProjectHandler(self, PackageManagerProject)
        self.project_handler.register()

    @asyncio.coroutine
    def run(self):
        pass

    @asyncio.coroutine
    def on_dts_state_change(self, state):
        """Handle DTS state change

        Take action according to current DTS state to transition application
        into the corresponding application state

        Arguments
            state - current dts state

        """
        switch = {
            rwdts.State.INIT: rwdts.State.REGN_COMPLETE,
            rwdts.State.CONFIG: rwdts.State.RUN,
        }

        handlers = {
            rwdts.State.INIT: self.init,
            rwdts.State.RUN: self.run,
        }

        # Transition application to next state
        handler = handlers.get(state, None)
        if handler is not None:
            yield from handler()

        # Transition dts to next state
        next_state = switch.get(state, None)
        if next_state is not None:
            self.dts.handle.set_state(next_state)

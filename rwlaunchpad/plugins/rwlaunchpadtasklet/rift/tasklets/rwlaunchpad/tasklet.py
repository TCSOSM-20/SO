
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

import asyncio

import tornado
import tornado.httputil
import tornado.httpserver
import tornado.platform.asyncio

import tornadostreamform.multipart_streamer as multipart_streamer

import gi
gi.require_version('RwDts', '1.0')
gi.require_version('RwcalYang', '1.0')
gi.require_version('RwTypes', '1.0')
gi.require_version('RwLaunchpadYang', '1.0')

from gi.repository import (
    RwDts as rwdts,
    RwLaunchpadYang as rwlaunchpad,
    RwcalYang as rwcal,
    RwTypes,
)

import rift.tasklets
import rift.mano.cloud
import rift.mano.config_agent
from rift.mano.utils.project import (
    ManoProject,
    ProjectHandler,
    get_add_delete_update_cfgs,
    DEFAULT_PROJECT,
    )
from rift.package import store

from . import uploader
from . import datacenters

MB = 1024 * 1024
GB = 1024 * MB
TB = 1024 * GB

MAX_BUFFER_SIZE = 1 * MB  # Max. size loaded into memory!
MAX_BODY_SIZE = 1 * MB  # Max. size loaded into memory!


class LaunchpadError(Exception):
    pass

class LpProjectNotFound(Exception):
    pass

class CatalogDtsHandler(object):
    def __init__(self, project, app):
        self.app = app
        self.reg = None
        self.project = project

    @property
    def log(self):
        return self.project.log

    @property
    def dts(self):
        return self.project.dts


class NsdCatalogDtsHandler(CatalogDtsHandler):
    XPATH = "C,/project-nsd:nsd-catalog/project-nsd:nsd"

    def add_nsd(self, nsd):
        self.log.debug('nsd-catalog-handler:add:{}'.format(nsd.id))
        if nsd.id not in self.project.nsd_catalog:
            self.project.nsd_catalog[nsd.id] = nsd
        else:
            self.log.error("nsd already in catalog: {}".format(nsd.id))

    def update_nsd(self, nsd):
        self.log.debug('nsd-catalog-handler:update:{}'.format(nsd.id))
        if nsd.id in self.project.nsd_catalog:
            self.project.nsd_catalog[nsd.id] = nsd
        else:
            self.log.error("unrecognized NSD: {}".format(nsd.id))

    def delete_nsd(self, nsd_id):
        self.log.debug('nsd-catalog-handler:delete:{}'.format(nsd_id))
        if nsd_id in self.project.nsd_catalog:
            del self.project.nsd_catalog[nsd_id]
        else:
            self.log.error("unrecognized NSD: {}".format(nsd_id))

        try:
            self.project.tasklet.nsd_package_store.delete_package(nsd_id)
        except store.PackageStoreError as e:
            self.log.warning("could not delete package from store: %s", str(e))

    @asyncio.coroutine
    def register(self):
        def apply_config(dts, acg, xact, action, _):
            if xact.xact is None:
                # When RIFT first comes up, an INSTALL is called with the current config
                # Since confd doesn't actally persist data this never has any data so
                # skip this for now.
                self.log.debug("No xact handle.  Skipping apply config")
                return

            add_cfgs, delete_cfgs, update_cfgs = get_add_delete_update_cfgs(
                    dts_member_reg=self.reg,
                    xact=xact,
                    key_name="id",
                    )

            # Handle Deletes
            for cfg in delete_cfgs:
                self.delete_nsd(cfg.id)

            # Handle Adds
            for cfg in add_cfgs:
                self.add_nsd(cfg)

            # Handle Updates
            for cfg in update_cfgs:
                self.update_nsd(cfg)

        self.log.debug("Registering for NSD catalog in project".
                       format(self.project.name))

        acg_handler = rift.tasklets.AppConfGroup.Handler(
                        on_apply=apply_config,
                        )

        with self.dts.appconf_group_create(acg_handler) as acg:
            xpath = self.project.add_project(NsdCatalogDtsHandler.XPATH)
            self.reg = acg.register(
                    xpath=xpath,
                    flags=rwdts.Flag.SUBSCRIBER,
                    )

    def deregister(self):
        if self.reg:
            self.reg.deregister()
            self.reg = None


class VnfdCatalogDtsHandler(CatalogDtsHandler):
    XPATH = "C,/project-vnfd:vnfd-catalog/project-vnfd:vnfd"

    def add_vnfd(self, vnfd):
        self.log.debug('vnfd-catalog-handler:add:{}'.format(vnfd.id))
        if vnfd.id not in self.project.vnfd_catalog:
            self.project.vnfd_catalog[vnfd.id] = vnfd

        else:
            self.log.error("VNFD already in catalog: {}".format(vnfd.id))

    def update_vnfd(self, vnfd):
        self.log.debug('vnfd-catalog-handler:update:{}'.format(vnfd.id))
        if vnfd.id in self.project.vnfd_catalog:
            self.project.vnfd_catalog[vnfd.id] = vnfd

        else:
            self.log.error("unrecognized VNFD: {}".format(vnfd.id))

    def delete_vnfd(self, vnfd_id):
        self.log.debug('vnfd-catalog-handler:delete:{}'.format(vnfd_id))
        if vnfd_id in self.project.vnfd_catalog:
            del self.project.vnfd_catalog[vnfd_id]
        else:
            self.log.error("unrecognized VNFD: {}".format(vnfd_id))

        try:
            self.project.tasklet.vnfd_package_store.delete_package(vnfd_id)
        except store.PackageStoreError as e:
            self.log.warning("could not delete package from store: %s", str(e))

    @asyncio.coroutine
    def register(self):
        def apply_config(dts, acg, xact, action, _):
            if xact.xact is None:
                # When RIFT first comes up, an INSTALL is called with the current config
                # Since confd doesn't actally persist data this never has any data so
                # skip this for now.
                self.log.debug("No xact handle.  Skipping apply config")
                return

            add_cfgs, delete_cfgs, update_cfgs = get_add_delete_update_cfgs(
                    dts_member_reg=self.reg,
                    xact=xact,
                    key_name="id",
                    )

            # Handle Deletes
            for cfg in delete_cfgs:
                self.delete_vnfd(cfg.id)

            # Handle Adds
            for cfg in add_cfgs:
                self.add_vnfd(cfg)

            # Handle Updates
            for cfg in update_cfgs:
                self.update_vnfd(cfg)

        self.log.debug("Registering for VNFD catalog in project {}".
                       format(self.project.name))

        acg_handler = rift.tasklets.AppConfGroup.Handler(
                        on_apply=apply_config,
                        )

        with self.dts.appconf_group_create(acg_handler) as acg:
            xpath = self.project.add_project(VnfdCatalogDtsHandler.XPATH)
            self.reg = acg.register(
                    xpath=xpath,
                    flags=rwdts.Flag.SUBSCRIBER,
                    )

    def deregister(self):
        if self.reg:
            self.reg.deregister()
            self.reg = None

class CfgAgentAccountHandlers(object):
    def __init__(self, dts, log, log_hdl, loop, project):
        self._dts = dts
        self._log = log
        self._log_hdl = log_hdl
        self._loop = loop
        self._project = project

        self._log.debug("creating config agent account config handler")
        self.cfg_agent_cfg_handler = rift.mano.config_agent.ConfigAgentSubscriber(
            self._dts, self._log, self._project,
            rift.mano.config_agent.ConfigAgentCallbacks(
                on_add_apply=self.on_cfg_agent_account_added,
                on_delete_apply=self.on_cfg_agent_account_deleted,
            )
        )

        self._log.debug("creating config agent account opdata handler")
        self.cfg_agent_operdata_handler = rift.mano.config_agent.CfgAgentDtsOperdataHandler(
            self._dts, self._log, self._loop, self._project
        )

    def on_cfg_agent_account_deleted(self, account):
        self._log.debug("config agent account deleted")
        self.cfg_agent_operdata_handler.delete_cfg_agent_account(account.name)

    def on_cfg_agent_account_added(self, account):
        self._log.debug("config agent account added")
        self.cfg_agent_operdata_handler.add_cfg_agent_account(account)

    @asyncio.coroutine
    def register(self):
        self.cfg_agent_cfg_handler.register()
        yield from self.cfg_agent_operdata_handler.register()

    def deregister(self):
        self.cfg_agent_operdata_handler.deregister()
        self.cfg_agent_cfg_handler.deregister()


class CloudAccountHandlers(object):
    def __init__(self, dts, log, log_hdl, loop, app, project):
        self._log = log
        self._log_hdl = log_hdl
        self._dts = dts
        self._loop = loop
        self._app = app
        self._project = project

        self._log.debug("Creating cloud account config handler for project {}".
                        format(project.name))
        self.cloud_cfg_handler = rift.mano.cloud.CloudAccountConfigSubscriber(
            self._dts, self._log, self._log_hdl, self._project,
            rift.mano.cloud.CloudAccountConfigCallbacks(
                on_add_apply=self.on_cloud_account_added,
                on_delete_apply=self.on_cloud_account_deleted,
            ),
        )

        self._log.debug("creating cloud account opdata handler")
        self.cloud_operdata_handler = rift.mano.cloud.CloudAccountDtsOperdataHandler(
            self._dts, self._log, self._loop, self._project,
        )

    def on_cloud_account_deleted(self, account_name):
        self._log.debug("cloud account deleted")
        self._app.accounts[self._project.name] = \
            list(self.cloud_cfg_handler.accounts.values())
        self.cloud_operdata_handler.delete_cloud_account(account_name)

    def on_cloud_account_added(self, account):
        self._log.debug("cloud account added")
        self._app.accounts[self._project.name] = \
            list(self.cloud_cfg_handler.accounts.values())
        self._log.debug("accounts: %s", self._app.accounts)
        self.cloud_operdata_handler.add_cloud_account(account)

    @asyncio.coroutine
    def register(self):
        self.cloud_cfg_handler.register()
        yield from self.cloud_operdata_handler.register()

    def deregister(self):
        self.cloud_cfg_handler.deregister()
        yield from self.cloud_operdata_handler.deregister()


class LaunchpadProject(ManoProject):

    def __init__(self, name, tasklet, **kw):
        super(LaunchpadProject, self).__init__(tasklet.log, name)
        self.update(tasklet)
        self._app = kw['app']

        self.config_handler = None
        self.nsd_catalog_handler = None
        self.vld_catalog_handler = None
        self.vnfd_catalog_handler = None
        self.cloud_handler = None
        self.datacenter_handler = None
        self.lp_config_handler = None
        self.account_handler = None

        self.nsd_catalog = dict()
        self.vld_catalog = dict()
        self.vnfd_catalog = dict()

    @property
    def dts(self):
        return self._dts

    @property
    def loop(self):
        return self._loop

    @asyncio.coroutine
    def register(self):
        self.log.debug("creating NSD catalog handler for project {}".format(self.name))
        self.nsd_catalog_handler = NsdCatalogDtsHandler(self, self._app)
        yield from self.nsd_catalog_handler.register()

        self.log.debug("creating VNFD catalog handler for project {}".format(self.name))
        self.vnfd_catalog_handler = VnfdCatalogDtsHandler(self, self._app)
        yield from self.vnfd_catalog_handler.register()

        self.log.debug("creating datacenter handler for project {}".format(self.name))
        self.datacenter_handler = datacenters.DataCenterPublisher(self.log, self.dts,
                                                                  self.loop, self)
        yield from self.datacenter_handler.register()

        self.log.debug("creating cloud account handler for project {}".format(self.name))
        self.cloud_handler = CloudAccountHandlers(self.dts, self.log, self.log_hdl,
                                                  self.loop, self._app, self)
        yield from self.cloud_handler.register()

        self.log.debug("creating config agent handler for project {}".format(self.name))
        self.config_handler = CfgAgentAccountHandlers(self.dts, self.log, self.log_hdl,
                                                      self.loop, self)
        yield from self.config_handler.register()

    def deregister(self):
        self.log.debug("De-register handlers for project: {}".format(self.name))
        self.config_handler.deregister()
        self.cloud_handler.deregister()
        self.datacenter_handler.deregister()
        self.vnfd_catalog_handler.deregister()
        self.nsd_catalog_handler.deregister()

    @asyncio.coroutine
    def delete_prepare(self):
        if self.nsd_catalog or self.vnfd_catalog or self.vld_catalog:
            return False
        return True

    @property
    def cloud_accounts(self):
        if self.cloud_handler is None:
            return list()

        return list(self.cloud_handler.cloud_cfg_handler.accounts.values())


class LaunchpadTasklet(rift.tasklets.Tasklet):
    UPLOAD_MAX_BODY_SIZE = MAX_BODY_SIZE
    UPLOAD_MAX_BUFFER_SIZE = MAX_BUFFER_SIZE
    UPLOAD_PORT = "4567"

    def __init__(self, *args, **kwargs):
        super(LaunchpadTasklet, self).__init__(*args, **kwargs)
        self.rwlog.set_category("rw-mano-log")
        self.rwlog.set_subcategory("launchpad")

        self.dts = None
        self.project_handler = None

        self.vnfd_package_store = store.VnfdPackageFilesystemStore(self.log)
        self.nsd_package_store = store.NsdPackageFilesystemStore(self.log)

        self.app = None
        self.server = None
        self.projects = {}
        print("LP Tasklet init")

    def _get_project(project=None):
        if project is None:
            project = DEFAULT_PROJECT

        if project in self.projects:
            return self.projects[project]

        msg = "Project {} not found".format(project)
        self._log.error(msg)
        raise LpProjectNotFound(msg)

    def nsd_catalog_get(self, project=None):
        return self._get_project(project=project).nsd_catalog

    def vnfd_catalog_get(self, project=None):
        return self._get_project(project=project).vnfd_catalog

    def get_cloud_accounts(self, project=None):
        return self._get_project(project=project).cloud_accounts

    def start(self):
        super(LaunchpadTasklet, self).start()
        self.log.info("Starting LaunchpadTasklet")

        self.log.debug("Registering with dts")
        self.dts = rift.tasklets.DTS(
                self.tasklet_info,
                rwlaunchpad.get_schema(),
                self.loop,
                self.on_dts_state_change
                )

        self.log.debug("Created DTS Api GI Object: %s", self.dts)

    def stop(self):
        try:
            self.server.stop()
            self.dts.deinit()
        except Exception:
            self.log.exception("Caught Exception in LP stop")
            raise

    def get_vnfd_catalog(self, project):
        return self.projects[project].vnfd_catalog

    def get_nsd_catalog(self, project):
        return self.projects[project].nsd_catalog

    @asyncio.coroutine
    def init(self):
        try:
            io_loop = rift.tasklets.tornado.TaskletAsyncIOLoop(asyncio_loop=self.loop)
            self.app = uploader.UploaderApplication.from_tasklet(self)
            yield from self.app.register()

            manifest = self.tasklet_info.get_pb_manifest()
            ssl_cert = manifest.bootstrap_phase.rwsecurity.cert
            ssl_key = manifest.bootstrap_phase.rwsecurity.key
            ssl_options = {
                "certfile": ssl_cert,
                "keyfile": ssl_key,
            }

            if manifest.bootstrap_phase.rwsecurity.use_ssl:
                self.server = tornado.httpserver.HTTPServer(
                    self.app,
                    max_body_size=LaunchpadTasklet.UPLOAD_MAX_BODY_SIZE,
                    io_loop=io_loop,
                    ssl_options=ssl_options,
                )

            else:
                self.server = tornado.httpserver.HTTPServer(
                    self.app,
                    max_body_size=LaunchpadTasklet.UPLOAD_MAX_BODY_SIZE,
                    io_loop=io_loop,
                )

            self.log.debug("Registering project handler")
            print("PJ: Registering project handler")
            self.project_handler = ProjectHandler(self, LaunchpadProject,
                                                  app=self.app)
            self.project_handler.register()

        except Exception as e:
            self.log.error("Exception : {}".format(e))
            self.log.exception(e)

    @asyncio.coroutine
    def run(self):
        self.server.listen(LaunchpadTasklet.UPLOAD_PORT)

    def on_instance_started(self):
        self.log.debug("Got instance started callback")

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

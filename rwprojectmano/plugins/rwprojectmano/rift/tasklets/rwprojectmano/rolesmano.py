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

"""
Project Manager tasklet is responsible for managing the Projects
configurations required for Role Based Access Control feature.
"""


import asyncio
from enum import Enum

import gi
gi.require_version('RwDts', '1.0')
gi.require_version('RwRbacInternalYang', '1.0')
from gi.repository import (
    RwDts as rwdts,
    ProtobufC,
    RwTypes,
    RwRbacInternalYang,
)

import rift.tasklets
from rift.tasklets.rwproject.project import (
    User,
    UserState,
    RoleKeys,
    RoleKeysUsers,
)
from rift.mano.utils.project import (
    NS_PROJECT,
    get_add_delete_update_cfgs,
)


from .projectmano import MANO_PROJECT_ROLES


class ProjectConfigSubscriber(object):
    """Config subscriber for rw-user config"""

    def __init__(self, project):
        self.project_name = project.name
        self._log = project.log
        self._dts = project.dts

        self.users = {}
        self.pub = RoleConfigPublisher(project)
        self.proj_roles = [role['mano-role'] for role in MANO_PROJECT_ROLES]

    def get_xpath(self):
        return "C,/{}[name='{}']/project-config/user".format(NS_PROJECT, self.project_name)


    def role_inst(self, role, keys=None):
        if not keys:
            keys = self.project_name

        r = RoleKeys()
        r.role = role.role
        r.keys = keys
        return r

    def delete_user(self, cfg):
        user = User().pb(cfg)
        self._log.error("Delete user {} for project {}".
                        format(user.key, self.project_name))
        if user.key in self.users:
            roles = self.users[user.key]
            for role_key in list(roles):
                self.delete_role(user, role_key)
            self.users.pop(user.key)

    def update_user(self, cfg):
        user = User().pb(cfg)
        self._log.error("Update user {} for project {}".
                        format(user.key, self.project_name))
        cfg_roles = {}
        for cfg_role in cfg.mano_role:
            r = self.role_inst(cfg_role)
            cfg_roles[r.key] = r

        if not user.key in self.users:
            self.users[user.key] = set()
        else:
            #Check if any roles are deleted for the user
            for role_key in list(self.users[user.key]):
                    if role_key not in cfg_roles:
                        self.delete_role(user, role_key)

        for role_key in cfg_roles.keys():
            if role_key not in self.users[user.key]:
                self.update_role(user, cfg_roles[role_key])

    def delete_role(self, user, role_key):
        self._log.error("Delete role {} for user {}".
                        format(role_key, user.key))
        user_key = user.key

        try:
            roles = self.users[user_key]
        except KeyError:
            roles = set()
            self.users[user.key] = roles

        if role_key in roles:
            roles.remove(role_key)
            self.pub.delete_role(role_key, user_key)

    def update_role(self, user, role):
        self._log.debug("Update role {} for user {}".
                        format(role.role, user.key))
        user_key = user.key

        try:
            roles = self.users[user.key]
        except KeyError:
            roles = set()
            self.users[user_key] = roles

        role_key = role.key

        if not role_key in roles:
            roles.add(role_key)
            self.pub.add_update_role(role_key, user_key)

    @asyncio.coroutine
    def register(self):
        @asyncio.coroutine
        def apply_config(dts, acg, xact, action, scratch):
            self._log.debug("Got user apply config (xact: %s) (action: %s)",
                            xact, action)

            if xact.xact is None:
                if action == rwdts.AppconfAction.INSTALL:
                    curr_cfg = self._reg.elements
                    for cfg in curr_cfg:
                        self._log.debug("Project being re-added after restart.")
                        self.add_user(cfg)
                else:
                    # When RIFT first comes up, an INSTALL is called with the current config
                    # Since confd doesn't actally persist data this never has any data so
                    # skip this for now.
                    self._log.debug("No xact handle.  Skipping apply config")

                return

            # TODO: There is user-name and user-domain as keys. Need to fix
            # this
            add_cfgs, delete_cfgs, update_cfgs = get_add_delete_update_cfgs(
                    dts_member_reg=self._reg,
                    xact=xact,
                    key_name="user_name",
                    )

            self._log.debug("Added: {}, Deleted: {}, Modified: {}".
                            format(add_cfgs, delete_cfgs, update_cfgs))
            # Handle Deletes
            for cfg in delete_cfgs:
                self.delete_user(cfg)

            # Handle Adds
            for cfg in add_cfgs:
                self.update_user(cfg)

            # Handle Updates
            for cfg in update_cfgs:
                self.update_user(cfg)

            return RwTypes.RwStatus.SUCCESS

        @asyncio.coroutine
        def on_prepare(dts, acg, xact, xact_info, ks_path, msg, scratch):
            """ Prepare callback from DTS for Project """

            action = xact_info.query_action

            self._log.debug("Project %s on_prepare config received (action: %s): %s",
                            self.project_name, xact_info.query_action, msg)

            user = User().pb(msg)
            if action in [rwdts.QueryAction.CREATE, rwdts.QueryAction.UPDATE]:
                if user.key in self.users:
                    self._log.debug("User {} update request".
                                    format(user.key))

                else:
                    self._log.debug("User {}: on_prepare add request".
                                    format(user.key))

                for role in msg.mano_role:
                    if role.role not in self.proj_roles:
                        errmsg = "Invalid MANO role {} for user {}". \
                                 format(role.role, user.key)
                        self._log.error(errmsg)
                        xact_info.send_error_xpath(RwTypes.RwStatus.FAILURE,
                                                   self.get_xpath(),
                                                   errmsg)
                        xact_info.respond_xpath(rwdts.XactRspCode.NACK)
                        return

            elif action == rwdts.QueryAction.DELETE:
                # Check if the user got deleted
                fref = ProtobufC.FieldReference.alloc()
                fref.goto_whole_message(msg.to_pbcm())
                if fref.is_field_deleted():
                    if user.key in self.users:
                        self._log.debug("User {} being deleted".format(user.key))
                    else:
                        self._log.warning("Delete on unknown user: {}".
                                          format(user.key))

            else:
                self._log.error("Action (%s) NOT SUPPORTED", action)
                xact_info.respond_xpath(rwdts.XactRspCode.NACK)
                return

            xact_info.respond_xpath(rwdts.XactRspCode.ACK)

        xpath = self.get_xpath()
        self._log.debug("Registering for project config using xpath: %s",
                        xpath,
                        )

        acg_handler = rift.tasklets.AppConfGroup.Handler(
                        on_apply=apply_config,
                        )

        with self._dts.appconf_group_create(acg_handler) as acg:
            self._reg = acg.register(
                    xpath=xpath,
                    flags=rwdts.Flag.SUBSCRIBER | rwdts.Flag.DELTA_READY | rwdts.Flag.CACHE,
                    on_prepare=on_prepare,
                    )

        yield from self.pub.register()
        self.pub.create_project_roles()

    def deregister(self):
        self._log.debug("De-registering DTS handler for project {}".
                        format(self.project_name))

        if self._reg:
            self._reg.deregister()
            self._reg = None

        self.pub.deregister()

class RoleState(Enum):
    """Role states"""
    NONE = 0
    NEW = 1
    INIT_DONE = 2
    ACTIVE = 3
    UPDATE = 4
    UPDATE_DONE = 5
    ERROR = 6
    DELETE = 7
    DELETE_DONE = 8


class RoleConfigPublisher(rift.tasklets.DtsConfigPublisher):

    def __init__(self, project):
        super().__init__(project._tasklet)
        self.project_name = project.name
        self.rbac_int = RwRbacInternalYang.YangData_RwRbacInternal_RwRbacInternal()
        self.roles = {}
        self.proj_roles = [role['mano-role'] for role in MANO_PROJECT_ROLES]
        self.proj_roles_published = False

    def get_xpath(self):
        return "D,/rw-rbac-internal:rw-rbac-internal/rw-rbac-internal:role"

    def role_xpath(self, role_key):
        return "D,/rw-rbac-internal:rw-rbac-internal/rw-rbac-internal:role" + \
            "[rw-rbac-internal:role='{}']".format(role_key[0]) + \
            "[rw-rbac-internal:keys='{}']".format(role_key[1])

    def role_user_xpath(self, role_key, user_key):
        return self.role_xpath(role_key) + \
            "/rw-rbac-internal:user" + \
            "[rw-rbac-internal:user-name='{}']".format(user_key[1]) + \
            "[rw-rbac-internal:user-domain='{}']".format(user_key[0])

    @classmethod
    def yang_state_str(cls, state):
        """ Return the state as a yang enum string """
        state_to_str_map = {RoleState.NONE: "none",
                            RoleState.NEW: "new",
                            RoleState.INIT_DONE: "init-done",
                            RoleState.ACTIVE: "active",
                            RoleState.UPDATE: "update",
                            RoleState.UPDATE_DONE: "update-done",
                            RoleState.ERROR: "error",
                            RoleState.DELETE: "delete",
                            RoleState.DELETE_DONE: "delete_done",
                            }
        return state_to_str_map[state]

    def create_project_roles(self):
        for name in self.proj_roles:
            role = RoleKeys()
            role.role = name
            role.keys = self.project_name
            self.create_project_role(role)

    def create_project_role(self, role):
        self.log.error("Create project role for {}: {}".
                       format(self.project_name, role.role))
        xpath = self.role_xpath(role.key)
        pb_role = self.pb_role(role)
        self._regh.update_element(xpath, pb_role)

    def delete_project_roles(self):
        for name in self.proj_roles:
            role = RoleKeys()
            role.role = name
            role.keys = self.project_name
            self.delete_project_role(role)

    def delete_project_role(self, role):
        self.log.error("Delete project role for {}: {}".
                       format(self.project_name, role.role))
        xpath = self.role_xpath(role.key)
        self._regh.delete_element(xpath)

    def create_role(self, role_key, user_key):
        return  RoleKeysUsers(role_key, user_key)

    def pb_role(self, role):

        pbRole = self.rbac_int.create_role()
        pbRole.role = role.role
        pbRole.keys = role.keys
        pbRole.state_machine.state = role.state.name

        return pbRole

    def pb_role_user(self, role, user):

        pbRole = self.rbac_int.create_role()
        pbRole.role = role.role
        pbRole.keys = role.keys

        pbUser = pbRole.create_user()
        pbUser.user_name = user.user_name
        pbUser.user_domain = user.user_domain
        pbUser.state_machine.state = user.state

        pbRole.user.append(pbUser)

        return pbRole

    def add_update_role(self, role_key, user_key):
        update = True
        try:
            role = self.roles[role_key]
        except KeyError:
            role = RoleKeysUsers(role_key)
            self.roles[role_key] = role
            update = False

        try:
            user = role.user(user_key)
        except KeyError:
            user = UserState(user_key)
            role.add_user(user)
            update = False

        if update:
            user.state = RoleConfigPublisher.yang_state_str(RoleState.UPDATE)
        else:
            user.state = RoleConfigPublisher.yang_state_str(RoleState.NEW)

        xpath = self.role_xpath(role_key)
        self.log.debug("update role: {} user: {} ".format(role_key, user_key))


        pb_role_user = self.pb_role_user(role, user)

        self._regh.update_element(xpath, pb_role_user)

    def delete_role(self, role_key, user_key):
        try:
            role = self.roles[role_key]
            user = role.user(user_key)
        except KeyError:
            return

        user.state = RoleConfigPublisher.yang_state_str(RoleState.DELETE)
        xpath = self.role_xpath(role_key)
        self.log.error("deleting role: {} user: {} ".format(role_key, user_key))

        pb_role = self.pb_role_user(role, user)
        self._regh.update_element(xpath, pb_role)

    def do_prepare(self, xact_info, action, ks_path, msg):
        """Handle on_prepare.
        """
        self.log.debug("do_prepare: action: {}, path: {} ks_path, msg: {}".format(action, ks_path, msg))

        xact_info.respond_xpath(rwdts.XactRspCode.ACK)

        # TODO: See if we need this as this would be called in the platform also
        # role_key = tuple([msg.role, msg.keys])

        # state = msg.state_machine.state
        # if state == 'init_done':
        #     msg.state_machine.state = 'active'
        #     xpath = self.role_xpath(role_key)
        #     self._regh.update_element(xpath, msg)

        # for user in msg.users:
        #     user_key = tuple([user.user_domain, user.user_name])
        #     state = user.state_machine.state
        #     if state == 'init_done':
        #         user.state_machine.state = 'active'
        #         xpath = self.role_xpath(role_key)
        #         self._regh.update_element(xpath, msg)

    def deregister(self):
        if self._regh:
            self.delete_project_roles()
            self._regh.deregister()
            self._regh = None

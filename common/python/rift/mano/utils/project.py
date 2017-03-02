#!/usr/bin/env python3

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


class ManoProjectError(Exception):
    pass


class ManoProjNameSetErr(ManoProjectError):
    pass


class ManoProjXpathNoProjErr(ManoProjectError):
    pass


class ManoProjXpathKeyErr(ManoProjectError):
    pass


class ManoProject(object):
    '''Class to handle the project name'''

    NS = 'rw-project'
    XPATH = '/{}:project'.format(NS)
    XPATH_LEN = len(XPATH)

    NAME = 'name'
    NAME_LEN = len(NAME)
    NS_NAME = '{}:{}'.format(NS, NAME)

    @classmethod
    def create_from_xpath(cls, xpath, log):
        name = cls.from_xpath(xpath, log)
        if name is None:
            return None

        proj = ManoProject(log, name=name)
        return proj

    @classmethod
    def from_xpath(cls, xpath, log):
        log.debug("Get project name from {}".format(xpath));

        if cls.XPATH in xpath:
            idx = xpath.find(cls.XPATH) + cls.XPATH_LEN
            if idx == -1:
                msg = "Project not found in XPATH: {}".format(xpath)
                log.error(msg)
                raise ManoProjXpathNoProjErr(msg)

            sub = xpath[idx:]
            sub = sub.strip()
            if (len(sub) < cls.NAME_LEN) or (sub[0] != '['):
                msg = "Project name not found in XPath: {}".format(xpath)
                log.error(msg)
                raise ManoProjXpathKeyErr(msg)

            sub = sub[1:].strip()
            idx = sub.find(cls.NS_NAME)
            if idx == -1:
                idx = sub.find(cls.NAME)
            if idx != 0:
                msg = "Project name not found in XPath: {}".format(xpath)
                log.error(msg)
                raise ManoProjXpathKeyErr(msg)

            idx = sub.find(']')
            if idx == -1:
                msg = "XPath is invalid: {}".format(xpath)
                log.error(msg)
                raise ManoProjXpathKeyErr(msg)

            sub = sub[:idx-1].strip()
            try:
                k, n = sub.split("=", 2)
                name = n.strip()
                if name is None:
                    msg = "Project name is empty in XPath".format(xpath)
                    log.error(msg)
                    raise ManoProjXpathKeyErr (msg)

                log.debug("Found project name {} from XPath {}".
                          format(name, xpath))
                return name

            except ValueError as e:
                msg = "Project name not found in XPath: {}, exception: {}" \
                      .format(xpath, e)
                log.exception(msg)
                raise ManoProjXpathKeyErr(msg)

    def __init__(self, log, name=None):
        self._log = log
        self._name = name

    @property
    def name(self):
        return self._name

    @name.setter
    def name(self, value):
        if self._name is None:
            self._name = value
        else:
            msg = "Project name already set to {}".format(self._name)
            self._log.error(msg)
            raise ManoProjNameSetErr(msg)

    def set_from_xpath(self, xpath):
        self.name = ManoProject.get_from_xpath(xpath, self._log)

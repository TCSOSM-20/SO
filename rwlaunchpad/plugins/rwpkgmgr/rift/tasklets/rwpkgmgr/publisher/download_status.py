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
# Author(s): Varun Prasad
# Creation Date: 09/25/2016
# 

import asyncio
import uuid

from gi.repository import (RwDts as rwdts)
import rift.mano.dts as mano_dts

import rift.downloader as url_downloader


class DownloadStatusPublisher(mano_dts.DtsHandler, url_downloader.DownloaderProtocol):

    def __init__(self, log, dts, loop, project):
        super().__init__(log, dts, loop, project)
        self.tasks = {}

    def xpath(self, download_id=None):
        return self._project.add_project("D,/rw-pkg-mgmt:download-jobs/rw-pkg-mgmt:job" +
                                         ("[download-id='{}']".
                                          format(download_id) if download_id else ""))

    @asyncio.coroutine
    def register(self):
        self.reg = yield from self.dts.register(xpath=self.xpath(),
                  flags=rwdts.Flag.PUBLISHER|rwdts.Flag.CACHE|rwdts.Flag.NO_PREP_READ)

        assert self.reg is not None

    def dergister(self):
        self._log.debug("De-registering download status for project {}".
                        format(self.project.name))
        if self.reg:
            self.reg.deregister()
            self.reg = None

    def on_download_progress(self, download_job_msg):
        """callback that triggers update.
        """
        key = download_job_msg.download_id
        # Trigger progess update
        self.reg.update_element(
                self.xpath(download_id=key),
                download_job_msg)

    def on_download_finished(self, download_job_msg):
        """callback that triggers update.
        """

        # clean up the local cache
        key = download_job_msg.download_id
        if key in self.tasks:
            del self.tasks[key]

        # Publish the final state
        self.reg.update_element(
                self.xpath(download_id=key),
                download_job_msg)

    @asyncio.coroutine
    def register_downloader(self, downloader):
        downloader.delegate = self
        future = self.loop.run_in_executor(None, downloader.download)
        self.tasks[downloader.download_id] = (downloader, future)

        return downloader.download_id

    @asyncio.coroutine
    def cancel_download(self, key):
        task, future = self.tasks[key]

        future.cancel()
        task.cancel_download()

    def stop(self):
        self.deregister()

        for task, future in self.tasks:
            task.cancel()
            future.cancel()

    def deregister(self):
        """ de-register with dts """
        if self.reg is not None:
            self.reg.deregister()
            self.reg = None

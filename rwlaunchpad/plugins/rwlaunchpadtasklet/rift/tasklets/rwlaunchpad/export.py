
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

import asyncio
import io
import os.path
import stat
import time
import uuid

import tornado.web

import rift.package.archive
import rift.package.checksums
import rift.package.package
import rift.package.store
import rift.package.image

from . import state
from . import message
from . import tosca

import gi
gi.require_version('RwPkgMgmtYang', '1.0')

from gi.repository import (
        RwPkgMgmtYang)
import rift.mano.dts as mano_dts


RPC_PACKAGE_EXPORT_ENDPOINT = RwPkgMgmtYang.YangOutput_RwPkgMgmt_PackageExport


class ExportStart(message.StatusMessage):
    def __init__(self):
        super().__init__("export-started", "export process started")


class ExportSuccess(message.StatusMessage):
    def __init__(self):
        super().__init__("export-success", "export process successfully completed")


class ExportFailure(message.StatusMessage):
    def __init__(self):
        super().__init__("export-failure", "export process failed")


class ExportError(message.ErrorMessage):
    def __init__(self, msg):
        super().__init__("update-error", msg)


class ExportSingleDescriptorOnlyError(ExportError):
    def __init__(self):
        super().__init__("Only a single descriptor can be exported")


class ArchiveExportError(Exception):
    pass


class DescriptorPackageArchiveExporter(object):
    def __init__(self, log):
        self._log = log

    def _create_archive_from_package(self, archive_hdl, package, open_fn):
        orig_open = package.open
        try:
            package.open = open_fn
            archive = rift.package.archive.TarPackageArchive.from_package(
                    self._log, package, archive_hdl
                    )
            return archive
        finally:
            package.open = orig_open

    def create_archive(self, archive_hdl, package, desc_json_str, serializer):
        """ Create a package archive from an existing package, descriptor messages,
            and a destination serializer.

        In order to stay flexible with the package directory structure and
        descriptor format, attempt to "augment" the onboarded package with the
        updated descriptor in the original format.  If the original package
        contained a checksum file, then recalculate the descriptor checksum.

        Arguments:
            archive_hdl - An open file handle with 'wb' permissions
            package - A DescriptorPackage instance
            desc_json_str - A descriptor (e.g. nsd, vnfd) protobuf message
            serializer - A destination serializer (e.g. VnfdSerializer)

        Returns:
            A TarPackageArchive

        Raises:
            ArchiveExportError - The exported archive failed to create

        """
        new_desc_msg = serializer.from_file_hdl(io.BytesIO(desc_json_str.encode()), ".json")
        _, dest_ext = os.path.splitext(package.descriptor_file)
        new_desc_hdl = io.BytesIO(serializer.to_string(new_desc_msg, dest_ext).encode())
        descriptor_checksum = rift.package.checksums.checksum(new_desc_hdl)

        checksum_file = None
        try:
            checksum_file = rift.package.package.PackageChecksumValidator.get_package_checksum_file(
                    package
                    )

        except FileNotFoundError:
            pass

        # Since we're going to intercept the open function to rewrite the descriptor
        # and checksum, save a handle to use below
        open_fn = package.open

        def create_checksum_file_hdl():
            with open_fn(checksum_file) as checksum_hdl:
                archive_checksums = rift.package.checksums.ArchiveChecksums.from_file_desc(
                        checksum_hdl
                        )

            archive_checksums[package.descriptor_file] = descriptor_checksum

            checksum_hdl = io.BytesIO(archive_checksums.to_string().encode())
            return checksum_hdl

        def open_wrapper(rel_path):
            """ Wraps the package open in order to rewrite the descriptor file and checksum """
            if rel_path == package.descriptor_file:
                return new_desc_hdl

            elif rel_path == checksum_file:
                return create_checksum_file_hdl()

            return open_fn(rel_path)

        archive = self._create_archive_from_package(archive_hdl, package, open_wrapper)

        return archive

    def export_package(self, package, export_dir, file_id, json_desc_str, dest_serializer):
        """ Export package as an archive to the export directory

        Arguments:
            package - A DescriptorPackage instance
            export_dir - The directory to export the package archive to
            file_id - A unique file id to name the archive as (i.e. <file_id>.tar.gz)
            json_desc_str - A descriptor (e.g. nsd, vnfd) json message string
            dest_serializer - A destination serializer (e.g. VnfdSerializer)

        Returns:
            The created archive path

        Raises:
            ArchiveExportError - The exported archive failed to create
        """
        try:
            os.makedirs(export_dir, exist_ok=True)
        except FileExistsError:
            pass

        archive_path = os.path.join(export_dir, file_id + ".tar.gz")
        with open(archive_path, 'wb') as archive_hdl:
            try:
                self.create_archive(
                    archive_hdl, package, json_desc_str, dest_serializer
                    )
            except Exception as e:
                os.remove(archive_path)
                msg = "Failed to create exported archive"
                self._log.error(msg)
                raise ArchiveExportError(msg) from e

        return archive_path


class ExportRpcHandler(mano_dts.AbstractRpcHandler):
    def __init__(self, application, catalog_map):
        """
        Args:
            application: UploaderApplication
            calalog_map: Dict containing Vnfds and Nsd onboarding.
        """
        super().__init__(application.log, application.dts, application.loop)

        self.application = application
        self.store_map = application.package_store_map
        self.exporter = application.exporter
        self.catalog_map = catalog_map

    @property
    def xpath(self):
        return "/rw-pkg-mgmt:package-export"

    @asyncio.coroutine
    def callback(self, ks_path, msg):
        transaction_id = str(uuid.uuid4())
        log = message.Logger(
                self.log,
                self.application.messages[transaction_id],
                )

        file_name = self.export(transaction_id, log, msg)

        rpc_out = RPC_PACKAGE_EXPORT_ENDPOINT.from_dict({
            'transaction_id': transaction_id,
            'filename': file_name})

        return rpc_out

    def export(self, transaction_id, log, msg):
        log.message(ExportStart())
        desc_type = msg.package_type.lower()

        if desc_type not in self.catalog_map:
            raise ValueError("Invalid package type: {}".format(desc_type))

        # Parse the IDs
        desc_id = msg.package_id
        catalog = self.catalog_map[desc_type](project=msg.project_name)

        if desc_id not in catalog:
            raise ValueError("Unable to find package ID: {}".format(desc_id))

        desc_msg = catalog[desc_id]

        # Get the schema for exporting
        schema = msg.export_schema.lower()

        # Get the grammar for exporting
        grammar = msg.export_grammar.lower()

        # Get the format for exporting
        format_ = msg.export_format.lower()

        filename = None

        if grammar == 'tosca':
            filename = "{}.zip".format(transaction_id)
            self.export_tosca(schema, format_, desc_type, desc_id, desc_msg, log, transaction_id)
            log.message(message.FilenameMessage(filename))
        else:
            filename = "{}.tar.gz".format(transaction_id)
            self.export_rift(schema, format_, desc_type, desc_id, desc_msg, log, transaction_id)
            log.message(message.FilenameMessage(filename))

        log.message(ExportSuccess())

        return filename

    def export_rift(self, schema, format_, desc_type, desc_id, desc_msg, log, transaction_id):
        convert = rift.package.convert
        schema_serializer_map = {
                "rift": {
                    "vnfd": convert.RwVnfdSerializer,
                    "nsd": convert.RwNsdSerializer,
                    },
                "mano": {
                    "vnfd": convert.VnfdSerializer,
                    "nsd": convert.NsdSerializer,
                    }
                }

        if schema not in schema_serializer_map:
            raise tornado.web.HTTPError(400, "unknown schema: {}".format(schema))

        if format_ != "yaml":
            log.warn("Only yaml format supported for export")

        if desc_type not in schema_serializer_map[schema]:
            raise tornado.web.HTTPError(400, "unknown descriptor type: {}".format(desc_type))

        # Use the rift superset schema as the source
        src_serializer = schema_serializer_map["rift"][desc_type]()

        dest_serializer = schema_serializer_map[schema][desc_type]()

        package_store = self.store_map[desc_type]

        # Attempt to get the package from the package store
        # If that fails, create a temporary package using the descriptor only
        try:
            package = package_store.get_package(desc_id)
        except rift.package.store.PackageNotFoundError:
            log.debug("stored package not found.  creating package from descriptor config")

            desc_yaml_str = src_serializer.to_yaml_string(desc_msg)
            with io.BytesIO(desc_yaml_str.encode()) as hdl:
                hdl.name = "{}__{}.yaml".format(desc_msg.id, desc_type)
                package = rift.package.package.DescriptorPackage.from_descriptor_file_hdl(
                    log, hdl
                    )

        self.exporter.export_package(
                package=package,
                export_dir=self.application.export_dir,
                file_id=transaction_id,
                json_desc_str=src_serializer.to_json_string(desc_msg),
                dest_serializer=dest_serializer,
                )

    def export_tosca(self, format_, schema, desc_type, desc_id, desc_msg, log, transaction_id):
        if format_ != "yaml":
            log.warn("Only yaml format supported for TOSCA export")

        if desc_type != "nsd":
            raise tornado.web.HTTPError(
                400,
                "NSD need to passed to generate TOSCA: {}".format(desc_type))

        def get_pkg_from_store(id_, type_):
            package = None
            # Attempt to get the package from the package store
            try:
                package_store = self.store_map[type_]
                package = package_store.get_package(id_)

            except rift.package.store.PackageNotFoundError:
                log.debug("stored package not found for {}.".format(id_))
            except rift.package.store.PackageStoreError:
                log.debug("stored package error for {}.".format(id_))

            return package

        pkg = tosca.ExportTosca()

        # Add NSD and related descriptors for exporting
        nsd_id = pkg.add_nsd(desc_msg, get_pkg_from_store(desc_id, "nsd"))

        catalog = self.catalog_map["vnfd"]
        for const_vnfd in desc_msg.constituent_vnfd:
            vnfd_id = const_vnfd.vnfd_id_ref
            if vnfd_id in catalog:
                pkg.add_vnfd(nsd_id,
                             catalog[vnfd_id],
                             get_pkg_from_store(vnfd_id, "vnfd"))
            else:
                raise tornado.web.HTTPError(
                    400,
                    "Unknown VNFD descriptor {} for NSD {}".
                    format(vnfd_id, nsd_id))

        # Create the archive.
        pkg.create_archive(transaction_id,
                           dest=self.application.export_dir)


class ExportStateHandler(state.StateHandler):
    STARTED = ExportStart
    SUCCESS = ExportSuccess
    FAILURE = ExportFailure


@asyncio.coroutine
def periodic_export_cleanup(log, loop, export_dir, period_secs=10 * 60, min_age_secs=30 * 60):
    """ Periodically cleanup old exported archives (.tar.gz files) in export_dir

    Arguments:
        log - A Logger instance
        loop - A asyncio event loop
        export_dir - The directory to cleanup old archives in
        period_secs - The number of seconds between clean ups
        min_age_secs - The minimum age of a archive to be eligible for cleanup

    """
    log.debug("Starting periodic export cleaning for export directory: %s", export_dir)

    # Create export dir if not created yet
    if not os.path.exists(export_dir):
        os.makedirs(export_dir)

    while True:
        yield from asyncio.sleep(period_secs, loop=loop)

        if not os.path.exists(export_dir):
            continue

        for file_name in os.listdir(export_dir):
            if not file_name.endswith(".tar.gz"):
                continue

            file_path = os.path.join(export_dir, file_name)

            try:
                file_stat = os.stat(file_path)
            except OSError as e:
                log.warning("Could not stat old exported archive: %s", str(e))
                continue

            file_age = time.time() - file_stat[stat.ST_MTIME]

            if file_age < min_age_secs:
                continue

            log.debug("Cleaning up old exported archive: %s", file_path)

            try:
                os.remove(file_path)
            except OSError as e:
                log.warning("Failed to remove old exported archive: %s", str(e))

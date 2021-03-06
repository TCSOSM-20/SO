#!/usr/bin/env python3

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


import argparse
import logging
import io
import os
import sys
import tempfile
import unittest
import xmlrunner

from rift.package.convert import (
        ProtoMessageSerializer,
        UnknownExtensionError,
        SerializationError,
        )

import gi
gi.require_version('RwpersonDbYang', '1.0')
gi.require_version('RwProjectPersonDbYang', '1.0')
gi.require_version('RwYang', '1.0')

from gi.repository import (
        RwpersonDbYang,
        RwProjectPersonDbYang,
        RwYang,
        )

from rift.package.convert import SerializationError


class TestSerializer(unittest.TestCase):
    def setUp(self):
        self._serializer = ProtoMessageSerializer(
                RwpersonDbYang,
                RwpersonDbYang.Person,
                RwProjectPersonDbYang,
                RwProjectPersonDbYang.YangData_RwProject_Project_Person,
                )

        self._sample_person = RwpersonDbYang.Person(name="Fred")
        self._project_person = RwProjectPersonDbYang.YangData_RwProject_Project_Person(name="Fred")
        self._model = RwYang.model_create_libyang()
        self._model.load_schema_ypbc(RwpersonDbYang.get_schema())

    def test_from_xml_file(self):
        sample_person_xml = self._sample_person.to_xml_v2(self._model)
        with io.StringIO(sample_person_xml) as file_hdl:
            person = self._serializer.from_file_hdl(file_hdl, ".xml")
            self.assertEqual(person, self._sample_person)

    def test_from_yaml_file(self):
        sample_person_yaml = self._sample_person.to_yaml(self._model)
        with io.StringIO(sample_person_yaml) as file_hdl:

            person = self._serializer.from_file_hdl(file_hdl, ".yml")
            self.assertEqual(person, self._project_person)

    def test_from_json_file(self):
        sample_person_json = self._sample_person.to_json(self._model)
        with io.StringIO(sample_person_json) as file_hdl:

            person = self._serializer.from_file_hdl(file_hdl, ".json")
            self.assertEqual(person, self._project_person)

    def test_unknown_file_extension(self):
        with io.StringIO("asdf") as file_hdl:
            with self.assertRaises(UnknownExtensionError):
                self._serializer.from_file_hdl(file_hdl, ".foo")

    def test_raises_serialization_error(self):
        with io.StringIO('</foo>') as file_hdl:
            with self.assertRaises(SerializationError):
                person = self._serializer.from_file_hdl(file_hdl, ".json")
                print(person)

    def test_to_json_string(self):
        json_str = self._serializer.to_json_string(self._sample_person)

        person = RwpersonDbYang.Person.from_json(self._model, json_str)
        self.assertEqual(person, self._sample_person)

    def test_to_json_string_invalid_type(self):
        with self.assertRaises(SerializationError):
            self._serializer.to_json_string(RwpersonDbYang.FlatPerson(name="bob"))


def main(argv=sys.argv[1:]):
    logging.basicConfig(format='TEST %(message)s')

    runner = xmlrunner.XMLTestRunner(output=os.environ["RIFT_MODULE_TEST"])
    parser = argparse.ArgumentParser()
    parser.add_argument('-v', '--verbose', action='store_true')
    parser.add_argument('-n', '--no-runner', action='store_true')

    args, unknown = parser.parse_known_args(argv)
    if args.no_runner:
        runner = None

    # Set the global logging level
    logging.getLogger().setLevel(logging.DEBUG if args.verbose else logging.ERROR)

    # The unittest framework requires a program name, so use the name of this
    # file instead (we do not want to have to pass a fake program name to main
    # when this is called from the interpreter).
    unittest.main(argv=[__file__] + unknown + ["-v"], testRunner=runner)

if __name__ == '__main__':
    main()

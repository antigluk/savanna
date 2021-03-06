# Copyright (c) 2013 Hortonworks, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os


class VersionHandlerFactory():
    versions = None
    modules = None
    initialized = False

    @staticmethod
    def get_instance():
        if not VersionHandlerFactory.initialized:
            src_dir = os.path.join(os.path.dirname(__file__), '')
            VersionHandlerFactory.versions = [name.replace('_', '.')
                                              for name in os.listdir(src_dir)
                                              if os.path.isdir(
                                              os.path.join(src_dir, name))]
            VersionHandlerFactory.modules = {}
            for version in VersionHandlerFactory.versions:
                module_name = 'savanna.plugins.hdp.versions.{0}.'\
                              'versionhandler'.format(
                              version.replace('.', '_'))
                module_class = getattr(
                    __import__(module_name, fromlist=['savanna']),
                    'VersionHandler')
                module = module_class()
                # would prefer to use __init__ or some constructor, but keep
                # getting exceptions...
                module._set_version(version)
                key = version.replace('_', '.')
                VersionHandlerFactory.modules[key] = module

            VersionHandlerFactory.initialized = True

        return VersionHandlerFactory()

    def get_versions(self):
        return VersionHandlerFactory.versions

    def get_version_handler(self, version):
        return VersionHandlerFactory.modules[version]

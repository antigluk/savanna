# Copyright (c) 2013 Red Hat, Inc.
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

import re

from oslo.config import cfg

from savanna import context
from savanna.utils.openstack import base


CONF = cfg.CONF

SWIFT_INTERNAL_PREFIX = "swift-internal://"

#TODO(tmckay): support swift-external in a future version
# SWIFT_EXTERNAL_PREFIX = "swift-external://"


def _get_service_address(service_type):
    ctx = context.current()
    identity_url = base.url_for(ctx.service_catalog, service_type)
    address_regexp = r"^\w+://(.+?)/"
    identity_host = re.search(address_regexp, identity_url).group(1)
    return identity_host


def retrieve_auth_url():
    """This function return auth url v2 api. Hadoop swift library doesn't
    support keystone v3 api.
    """
    protocol = CONF.os_auth_protocol
    host = _get_service_address('identity')

    return "%s://%s/v2.0/" % (protocol, host)

# Copyright (c) 2013 Mirantis Inc.
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

import datetime

from novaclient import exceptions as nova_exceptions
from oslo.config import cfg
import six

from savanna import conductor as c
from savanna import context
from savanna.openstack.common import excutils
from savanna.openstack.common import log as logging
from savanna.service import networks
from savanna.service import volumes
from savanna.utils import general as g
from savanna.utils.openstack import nova


conductor = c.API
CONF = cfg.CONF
LOG = logging.getLogger(__name__)


class SavannaInfrastructureEngine(object):
    def create_cluster(self, cluster):
        _create_cluster(cluster)

    def scale_cluster(self, cluster, node_group_id_map):
        return _scale_cluster(cluster, node_group_id_map)

    def shutdown_cluster(self, cluster):
        _shutdown_cluster(cluster)

    def get_instances(self, cluster, instances_ids=None):
        return _get_instances(cluster, instances_ids)

    def clean_cluster_from_empty_ng(self, cluster):
        _clean_cluster_from_empty_ng(cluster)

    def get_node_group_image_username(self, node_group):
        return _get_node_group_image_username(node_group)


def _create_cluster(cluster):
    ctx = context.ctx()
    try:
        # create all instances
        cluster = conductor.cluster_update(ctx, cluster,
                                           {"status": "Spawning"})
        LOG.info(g.format_cluster_status(cluster))
        _create_instances(cluster)

        # wait for all instances are up and networks ready
        cluster = conductor.cluster_update(ctx, cluster, {"status": "Waiting"})
        LOG.info(g.format_cluster_status(cluster))

        instances = _get_instances(cluster)

        _await_active(cluster, instances)

        _assign_floating_ips(instances)

        _await_networks(cluster, instances)

        cluster = conductor.cluster_get(ctx, cluster)

        # attach volumes
        volumes.attach(cluster)

        # prepare all instances
        cluster = conductor.cluster_update(ctx, cluster,
                                           {"status": "Preparing"})
        LOG.info(g.format_cluster_status(cluster))

        _configure_instances(cluster)
    except Exception as ex:
        LOG.warn("Can't start cluster '%s' (reason: %s)", cluster.name, ex)
        with excutils.save_and_reraise_exception():
            cluster = conductor.cluster_update(ctx, cluster,
                                               {"status": "Error",
                                                "status_description": str(ex)})
            LOG.info(g.format_cluster_status(cluster))
            _rollback_cluster_creation(cluster, ex)


def _get_instances(cluster, instances_ids=None):
    inst_map = {}
    for node_group in cluster.node_groups:
        for instance in node_group.instances:
            inst_map[instance.id] = instance

    if instances_ids is not None:
        return [inst_map[id] for id in instances_ids]
    else:
        return [v for v in six.itervalues(inst_map)]


def _scale_cluster(cluster, node_group_id_map):
    ctx = context.ctx()

    instance_ids = []
    try:
        instance_ids = _scale_cluster_instances(cluster, node_group_id_map)

        cluster = conductor.cluster_get(ctx, cluster)
        _clean_cluster_from_empty_ng(cluster)

        cluster = conductor.cluster_get(ctx, cluster)
        instances = _get_instances(cluster, instance_ids)

        _await_active(cluster, instances)

        _assign_floating_ips(instances)

        _await_networks(cluster, instances)

        cluster = conductor.cluster_get(ctx, cluster)

        volumes.attach_to_instances(_get_instances(cluster, instance_ids))

    except Exception as ex:
        LOG.warn("Can't scale cluster '%s' (reason: %s)", cluster.name, ex)
        with excutils.save_and_reraise_exception():
            cluster = conductor.cluster_get(ctx, cluster)
            _rollback_cluster_scaling(cluster,
                                      _get_instances(cluster, instance_ids),
                                      ex)
            instance_ids = []

            cluster = conductor.cluster_get(ctx, cluster)
            _clean_cluster_from_empty_ng(cluster)
            if cluster.status == 'Decommissioning':
                cluster = conductor.cluster_update(ctx, cluster,
                                                   {"status": "Error"})
            else:
                cluster = conductor.cluster_update(ctx, cluster,
                                                   {"status": "Active"})

            LOG.info(g.format_cluster_status(cluster))

    # we should be here with valid cluster: if instances creation
    # was not successful all extra-instances will be removed above
    if instance_ids:
        _configure_instances(cluster)
    return instance_ids


def _generate_anti_affinity_groups(cluster):
    aa_groups = {}

    for node_group in cluster.node_groups:
        for instance in node_group.instances:
            if instance.instance_id:
                for process in node_group.node_processes:
                    if process in cluster.anti_affinity:
                        aa_group = aa_groups.get(process, [])
                        aa_group.append(instance.instance_id)
                        aa_groups[process] = aa_group

    return aa_groups


def _create_instances(cluster):
    ctx = context.ctx()

    aa_groups = {}

    for node_group in cluster.node_groups:
        count = node_group.count
        conductor.node_group_update(ctx, node_group, {'count': 0})
        userdata = _generate_user_data_script(node_group)
        for idx in xrange(1, count + 1):
            _run_instance(cluster, node_group, idx, aa_groups, userdata)


def _scale_cluster_instances(cluster, node_group_id_map):
    ctx = context.ctx()
    aa_groups = _generate_anti_affinity_groups(cluster)
    instances_to_delete = []
    node_groups_to_enlarge = []

    for node_group in cluster.node_groups:
        new_count = node_group_id_map[node_group.id]

        if new_count < node_group.count:
            instances_to_delete += node_group.instances[new_count:
                                                        node_group.count]
        elif new_count > node_group.count:
            node_groups_to_enlarge.append(node_group)

    if instances_to_delete:
        cluster = conductor.cluster_update(ctx, cluster,
                                           {"status": "Deleting Instances"})
        LOG.info(g.format_cluster_status(cluster))

        for instance in instances_to_delete:
            _shutdown_instance(instance)

    cluster = conductor.cluster_get(ctx, cluster)

    instances_to_add = []
    if node_groups_to_enlarge:
        cluster = conductor.cluster_update(ctx, cluster,
                                           {"status": "Adding Instances"})
        LOG.info(g.format_cluster_status(cluster))
        for node_group in node_groups_to_enlarge:
            count = node_group_id_map[node_group.id]
            userdata = _generate_user_data_script(node_group)
            for idx in xrange(node_group.count + 1, count + 1):
                instance_id = _run_instance(cluster, node_group, idx,
                                            aa_groups, userdata)
                instances_to_add.append(instance_id)

    return instances_to_add


def _find_by_id(lst, id):
    for obj in lst:
        if obj.id == id:
            return obj

    return None


def _run_instance(cluster, node_group, idx, aa_groups, userdata):
    """Create instance using nova client and persist them into DB."""
    ctx = context.ctx()
    name = '%s-%s-%03d' % (cluster.name, node_group.name, idx)

    # aa_groups: node process -> instance ids
    aa_ids = []
    for node_process in node_group.node_processes:
        aa_ids += aa_groups.get(node_process) or []

    # create instances only at hosts w/ no instances w/ aa-enabled processes
    hints = {'different_host': list(set(aa_ids))} if aa_ids else None

    if CONF.use_neutron:
        net_id = cluster.neutron_management_network
        nics = [{"net-id": net_id, "v4-fixed-ip": ""}]

        nova_instance = nova.client().servers.create(
            name, node_group.get_image_id(), node_group.flavor_id,
            scheduler_hints=hints, userdata=userdata,
            key_name=cluster.user_keypair_id,
            nics=nics)
    else:
        nova_instance = nova.client().servers.create(
            name, node_group.get_image_id(), node_group.flavor_id,
            scheduler_hints=hints, userdata=userdata,
            key_name=cluster.user_keypair_id)

    instance_id = conductor.instance_add(ctx, node_group,
                                         {"instance_id": nova_instance.id,
                                          "instance_name": name})
    # save instance id to aa_groups to support aa feature
    for node_process in node_group.node_processes:
        if node_process in cluster.anti_affinity:
            aa_group_ids = aa_groups.get(node_process, [])
            aa_group_ids.append(nova_instance.id)
            aa_groups[node_process] = aa_group_ids

    return instance_id


def _generate_user_data_script(node_group):
    script_template = """#!/bin/bash
echo "%(public_key)s" >> %(user_home)s/.ssh/authorized_keys
"""

    username = _get_node_group_image_username(node_group)
    if username == "root":
        user_home = "/root/"
    else:
        user_home = "/home/%s/" % username

    cluster = node_group.cluster

    return script_template % {
        "public_key": cluster.management_public_key,
        "user_home": user_home
    }


def _assign_floating_ips(instances):
    for instance in instances:
        node_group = instance.node_group
        if node_group.floating_ip_pool:
            networks.assign_floating_ip(instance.instance_id,
                                        node_group.floating_ip_pool)


def _await_networks(cluster, instances):
    if not instances:
        return

    ips_assigned = set()
    while len(ips_assigned) != len(instances):
        if not g.check_cluster_exists(instances[0].node_group.cluster):
            return
        for instance in instances:
            if instance.id not in ips_assigned:
                if networks.init_instances_ips(instance):
                    ips_assigned.add(instance.id)

        context.sleep(1)

    LOG.info("Cluster '%s': all instances have IPs assigned" % cluster.id)

    ctx = context.ctx()
    cluster = conductor.cluster_get(ctx, instances[0].node_group.cluster)
    instances = _get_instances(cluster, ips_assigned)

    with context.ThreadGroup() as tg:
        for instance in instances:
            tg.spawn("wait-for-ssh-%s" % instance.instance_name,
                     _wait_until_accessible, instance)

    LOG.info("Cluster '%s': all instances are accessible" % cluster.id)


def _await_active(cluster, instances):
    """Await all instances are in Active status and available."""
    if not instances:
        return

    active_ids = set()
    while len(active_ids) != len(instances):
        if not g.check_cluster_exists(instances[0].node_group.cluster):
            return
        for instance in instances:
            if instance.id not in active_ids:
                if _check_if_active(instance):
                    active_ids.add(instance.id)

        context.sleep(1)

    LOG.info("Cluster '%s': all instances are active" % cluster.id)


def _check_if_active(instance):

    server = nova.get_instance_info(instance)
    if server.status == 'ERROR':
        # TODO(slukjanov): replace with specific error
        raise RuntimeError("node %s has error status" % server.name)

    return server.status == 'ACTIVE'


def _wait_until_accessible(instance):
    while True:
        try:
            # check if ssh is accessible and cloud-init
            # script is finished generating authorized_keys
            exit_code, stdout = instance.remote().execute_command(
                "ls .ssh/authorized_keys", raise_when_error=False)

            if exit_code == 0:
                LOG.debug('Instance %s is accessible' % instance.instance_name)
                return
        except Exception as ex:
            LOG.debug("Can't login to node %s (%s), reason %s",
                      instance.instance_name, instance.management_ip, ex)

        context.sleep(5)

        if not g.check_cluster_exists(instance.node_group.cluster):
            return


def _configure_instances(cluster):
    """Configure active instances.

    * generate /etc/hosts
    * setup passwordless login
    * etc.
    """
    hosts_file = _generate_etc_hosts(cluster)

    with context.ThreadGroup() as tg:
        for node_group in cluster.node_groups:
            for instance in node_group.instances:
                tg.spawn("configure-instance-%s" % instance.instance_name,
                         _configure_instance, instance, hosts_file)


def _configure_instance(instance, hosts_file):
    LOG.debug('Configuring instance %s' % instance.instance_name)

    with instance.remote() as r:
        r.write_file_to('etc-hosts', hosts_file)
        r.execute_command('sudo mv etc-hosts /etc/hosts')


def _generate_etc_hosts(cluster):
    hosts = "127.0.0.1 localhost\n"
    for node_group in cluster.node_groups:
        for instance in node_group.instances:
            hosts += "%s %s %s\n" % (instance.internal_ip,
                                     instance.fqdn(),
                                     instance.hostname())

    return hosts


def _rollback_cluster_creation(cluster, ex):
    """Shutdown all instances and update cluster status."""
    LOG.info("Cluster '%s' creation rollback (reason: %s)", cluster.name, ex)

    _shutdown_cluster(cluster)


def _rollback_cluster_scaling(cluster, instances, ex):
    """Attempt to rollback cluster scaling."""
    LOG.info("Cluster '%s' scaling rollback (reason: %s)", cluster.name, ex)
    try:
        volumes.detach_from_instances(instances)
    finally:
        for i in instances:
            _shutdown_instance(i)


def _clean_job_executions(cluster):
    ctx = context.ctx()
    for je in conductor.job_execution_get_all(ctx, cluster_id=cluster.id):
        update = {"cluster_id": None,
                  "end_time": datetime.datetime.now()}
        conductor.job_execution_update(ctx, je, update)


def _shutdown_instances(cluster):
    for node_group in cluster.node_groups:
        for instance in node_group.instances:
            _shutdown_instance(instance)


def _shutdown_instance(instance):
    ctx = context.ctx()

    if instance.node_group.floating_ip_pool:
        try:
            networks.delete_floating_ip(instance.instance_id)
        except nova_exceptions.NotFound:
            LOG.warn("Attempted to delete non-existent floating IP in "
                     "pool %s from instance %s",
                     instance.node_group.floating_ip_pool,
                     instance.instance_id)

    try:
        nova.client().servers.delete(instance.instance_id)
    except nova_exceptions.NotFound:
        LOG.warn("Attempted to delete non-existent instance %s",
                 instance.instance_id)

    conductor.instance_remove(ctx, instance)


def _shutdown_cluster(cluster):
    """Shutdown specified cluster and all related resources."""
    try:
        volumes.detach(cluster)
    finally:
        _shutdown_instances(cluster)
        _clean_job_executions(cluster)


def _clean_cluster_from_empty_ng(cluster):
    ctx = context.ctx()
    for ng in cluster.node_groups:
        if ng.count == 0:
            conductor.node_group_remove(ctx, ng)


def _get_node_group_image_username(node_group):
    image_id = node_group.get_image_id()
    return nova.client().images.get(image_id).username

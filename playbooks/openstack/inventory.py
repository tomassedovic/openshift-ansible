#!/usr/bin/env python
"""
This is an Ansible dynamic inventory for OpenStack.

It requires your OpenStack credentials to be set in clouds.yaml or your shell
environment.

"""

from __future__ import print_function

from collections import Mapping
import json
import os

import shade


def base_openshift_inventory(cluster_hosts):
    '''Set the base openshift inventory.'''
    inventory = {}

    masters = [server.name for server in cluster_hosts
               if server.metadata['host-type'] == 'master']

    etcd = [server.name for server in cluster_hosts
            if server.metadata['host-type'] == 'etcd']
    if not etcd:
        etcd = masters

    infra_hosts = [server.name for server in cluster_hosts
                   if server.metadata['host-type'] == 'node' and
                   server.metadata['sub-host-type'] == 'infra']

    app = [server.name for server in cluster_hosts
           if server.metadata['host-type'] == 'node' and
           server.metadata['sub-host-type'] == 'app']

    cns = [server.name for server in cluster_hosts
           if server.metadata['host-type'] == 'cns']

    nodes = list(set(masters + infra_hosts + app + cns))

    dns = [server.name for server in cluster_hosts
           if server.metadata['host-type'] == 'dns']

    load_balancers = [server.name for server in cluster_hosts
                      if server.metadata['host-type'] == 'lb']

    osev3 = list(set(nodes + etcd + load_balancers))

    inventory['cluster_hosts'] = {'hosts': [s.name for s in cluster_hosts]}
    inventory['OSEv3'] = {'hosts': osev3}
    inventory['masters'] = {'hosts': masters}
    inventory['etcd'] = {'hosts': etcd}
    inventory['nodes'] = {'hosts': nodes}
    inventory['infra_hosts'] = {'hosts': infra_hosts}
    inventory['app'] = {'hosts': app}
    inventory['glusterfs'] = {'hosts': cns}
    inventory['dns'] = {'hosts': dns}
    inventory['lb'] = {'hosts': load_balancers}

    return inventory


def get_docker_storage_mountpoints(volumes):
    '''Check volumes to see if they're being used for docker storage'''
    docker_storage_mountpoints = {}
    for volume in volumes:
        if volume.metadata.get('purpose') == "openshift_docker_storage":
            for attachment in volume.attachments:
                if attachment.server_id in docker_storage_mountpoints:
                    docker_storage_mountpoints[attachment.server_id].append(attachment.device)
                else:
                    docker_storage_mountpoints[attachment.server_id] = [attachment.device]
    return docker_storage_mountpoints


def build_inventory():
    '''Build the dynamic inventory.'''
    cloud = shade.openstack_cloud()

    # TODO(shadower): filter the servers based on the `OPENSHIFT_CLUSTER`
    # environment variable.
    cluster_hosts = [
        server for server in cloud.list_servers()
        if 'metadata' in server and 'clusterid' in server.metadata]

    inventory = base_openshift_inventory(cluster_hosts)

    for server in cluster_hosts:
        if 'group' in server.metadata:
            group = server.metadata.group
            if group not in inventory:
                inventory[group] = {'hosts': []}
            inventory[group]['hosts'].append(server.name)

    inventory['_meta'] = {'hostvars': {}}

    # cinder volumes used for docker storage
    docker_storage_mountpoints = get_docker_storage_mountpoints(cloud.list_volumes())

    for server in cluster_hosts:
        ssh_ip_address = server.public_v4 or server.private_v4
        hostvars = {
            'ansible_host': ssh_ip_address
        }

        public_v4 = server.public_v4 or server.private_v4
        if public_v4:
            hostvars['public_v4'] = server.public_v4
            hostvars['openshift_public_ip'] = server.public_v4
        # TODO(shadower): what about multiple networks?
        if server.private_v4:
            hostvars['private_v4'] = server.private_v4
            hostvars['openshift_ip'] = server.private_v4

            # NOTE(shadower): Yes, we set both hostname and IP to the private
            # IP address for each node. OpenStack doesn't resolve nodes by
            # name at all, so using a hostname here would require an internal
            # DNS which would complicate the setup and potentially introduce
            # performance issues.
            hostvars['openshift_hostname'] = server.metadata.get(
                'openshift_hostname', server.private_v4)
        hostvars['openshift_public_hostname'] = server.name

        if server.metadata['host-type'] == 'cns':
            hostvars['glusterfs_devices'] = ['/dev/nvme0n1']

        node_labels = server.metadata.get('node_labels')
        # NOTE(shadower): the node_labels value must be a dict not string
        if not isinstance(node_labels, Mapping):
            node_labels = json.loads(node_labels)

        if node_labels:
            hostvars['openshift_node_labels'] = node_labels

        # check for attached docker storage volumes
        if 'os-extended-volumes:volumes_attached' in server:
            if server.id in docker_storage_mountpoints:
                hostvars['docker_storage_mountpoints'] = ' '.join(docker_storage_mountpoints[server.id])

        inventory['_meta']['hostvars'][server.name] = hostvars

    kuryr_vars = _get_kuryr_vars(cloud)
    if kuryr_vars:
        inventory['OSEv3']['vars'] = kuryr_vars

    return inventory


def _get_kuryr_vars(cloud_client):
    """Returns a dictionary of Kuryr variables resulting of heat stacking"""
    # TODO: Filter the cluster stack with tags once it is supported in shade
    cluster_name = os.getenv('OPENSHIFT_CLUSTER', 'openshift-cluster')

    stack = cloud_client.get_stack(cluster_name)
    if stack is None or stack['stack_status'] not in (
            'CREATE_COMPLETE', 'UPDATE_COMPLETE'):
        return None

    data = {}
    for output in stack['outputs']:
        data[output['output_key']] = output['output_value']

    settings = {}
    settings['kuryr_openstack_pod_subnet_id'] = data['pod_subnet']
    settings['kuryr_openstack_worker_nodes_subnet_id'] = data['vm_subnet']
    settings['kuryr_openstack_service_subnet_id'] = data['service_subnet']
    settings['kuryr_openstack_pod_sg_id'] = data['pod_access_sg_id']
    settings['kuryr_openstack_pod_project_id'] = (
        cloud_client.current_project_id)

    settings['kuryr_openstack_auth_url'] = cloud_client.auth['auth_url']
    settings['kuryr_openstack_username'] = cloud_client.auth['username']
    settings['kuryr_openstack_password'] = cloud_client.auth['password']
    if 'user_domain_id' in cloud_client.auth:
        settings['kuryr_openstack_user_domain_name'] = (
            cloud_client.auth['user_domain_id'])
    else:
        settings['kuryr_openstack_user_domain_name'] = (
            cloud_client.auth['user_domain_name'])
    # FIXME(apuimedo): consolidate kuryr controller credentials into the same
    #                  vars the openstack playbook uses.
    settings['kuryr_openstack_project_id'] = cloud_client.current_project_id
    if 'project_domain_id' in cloud_client.auth:
        settings['kuryr_openstack_project_domain_name'] = (
            cloud_client.auth['project_domain_id'])
    else:
        settings['kuryr_openstack_project_domain_name'] = (
            cloud_client.auth['project_domain_name'])
    return settings


if __name__ == '__main__':
    print(json.dumps(build_inventory(), indent=4, sort_keys=True))

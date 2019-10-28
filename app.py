#!/usr/bin/env python3

import copy
import json
import logging
import os
import signal
import sys
import threading
import time
import traceback

import kubernetes
import openshift
import openshift.dynamic
import urllib3
import yaml
#from benedict import benedict
from kubernetes import client, config


def dumpstacks(signal, frame):
    id2name = dict([(th.ident, th.name) for th in threading.enumerate()])
    code = []
    for threadId, stack in sys._current_frames().items():
        code.append("\n# Thread: %s(%d)" % (id2name.get(threadId,""), threadId))
        for filename, lineno, name, line in traceback.extract_stack(stack):
            code.append('File: "%s", line %d, in %s' % (filename, lineno, name))
            if line:
                code.append("  %s" % (line.strip()))
    print("\n".join(code))


class AbstractWatcher(threading.Thread):
    def __init__(self, resource_client, namespace = None):
        super().__init__()
        self.resource_client = resource_client
        self.namespace = namespace

    def run(self):
        # https://kubernetes.io/docs/reference/using-api/api-concepts/#efficient-detection-of-changes
        while True:
            resources = self.resource_client.get(namespace=self.namespace).to_dict()
            self.reconcile_resources(resources)

            resource_version = resources['metadata']['resourceVersion']
            logging.info(f"Watching {self.resource_client.kind} in namespace '{self.namespace}' with resource version newer than '{resource_version}'")
            for event in self.resource_client.watch(namespace=self.namespace, resource_version=resource_version):
                #obj = benedict(event["raw_object"], keypath_separator=',')
                obj = event["raw_object"]
                operation = event['type']
                if operation == 'ERROR':
                    if obj.get('code') != 410:  # 410 is expected, meaning the watch expired
                        logging.error(event["raw_object"])
                        time.sleep(1)  # Prevent busy looping
                    break

                self.reconcile_resource(obj, operation)

    def reconcile_resources(self, resources):
        for resource in resources['items']:
            #self.reconcile_resource(benedict(resource, keypath_separator=','), 'ADDED')
            self.reconcile_resource(resource, 'ADDED')
            #self.reconcile_resource(openshift.dynamic.client.ResourceInstance(self.resource, resource), 'ADDED')

    def reconcile_resource(self, resource, operation):
        return


class ClusterRoleWatcher(AbstractWatcher):

    def __init__(self, openshift_client):
        self.v1_cluster_role = openshift_client.resources.get(group='rbac.authorization.k8s.io', api_version='v1', kind='ClusterRole')
        super().__init__(self.v1_cluster_role)

    def reconcile_resource(self, cluster_role, operation):
        # if operation == 'DELETED' and cluster_role['metadata']['name'] in ('restricted_admin', 'restricted_edit'):
        #     restricted_cluster_role_name = cluster_role['metadata']['name']
        #     cluster_role_name = restricted_cluster_role_name.partition('_')[0]
        if operation != 'DELETED' and cluster_role['metadata']['name'] in ('admin', 'edit'):
            cluster_role_name = cluster_role['metadata']['name']
            restricted_cluster_role_name = 'restricted_' + cluster_role_name
        # else:
        #     return

            logging.info(f"Reconciling ClusterRole '{restricted_cluster_role_name}'")

            #cluster_role.metadata.name = 'restricted_' + cluster_role.metadata.name
            restricted_rules = []
            for rule in cluster_role['rules']:
                if any(apiGroup in rule['apiGroups'] for apiGroup in ('route.openshift.io', 'networking.k8s.io')):
                    rule['verbs'][:] = [verb for verb in rule['verbs'] if verb in ('get', 'list', 'watch')]

                if rule['verbs']:
                    restricted_rules.append(rule)

            restricted_cluster_role = {
                #'kind': 'ClusterRole',
                #'apiVersion': cluster_role['apiVersion'],
                'metadata': {
                    'name': restricted_cluster_role_name
                },
                'rules': restricted_rules #[rule for rule in cluster_role['rules'] if not any(apiGroup in rule['apiGroups'] for apiGroup in ('route.openshift.io', 'networking.k8s.io'))]
            }

            #print(yaml.dump(restricted_cluster_role, default_flow_style=False, width=float("inf")))
            #print(json.dumps(restricted_cluster_role))
                #restricted_cluster_role['rules'] = [rule for rule in cluster_role.rules if not any(apiGroup in rule.apiGroups for apiGroup in ('route.openshift.io', 'networking.k8s.io'))]
                #cluster_role.rules[:] = [rule for rule in cluster_role.rules if not any(apiGroup in rule.apiGroups for apiGroup in ('route.openshift.io', 'networking.k8s.io'))]
                #cluster_role.metadata.resourceVersion = None
                #cluster_role.metadata.uid = None
                #cluster_role.metadata.annotations = []
                #cluster_role.aggregationRule = None
                #print(cluster_role)
            #try:
            self.v1_cluster_role.apply(body=restricted_cluster_role)
        #except Exception:
        #    pass


class RoleBindingWatcher(AbstractWatcher):

    def __init__(self, openshift_client, namespace):
        self.v1_role_binding = openshift_client.resources.get(group='rbac.authorization.k8s.io', api_version='v1', kind='RoleBinding')
        super().__init__(self.v1_role_binding, namespace)

    def reconcile_resource(self, role_binding, operation):
        #logging.info(f"Reconciling RoleBinding '{role_binding.metadata.name}'")
        if operation != 'DELETED' and not role_binding['roleRef'].get('namespace') and role_binding['roleRef']['name'] in ('admin', 'edit'):
            role_binding_name = role_binding['metadata']['name']
            restricted_role_binding_name = 'restricted_' + role_binding_name
            logging.info(f"Reconciling RoleBinding '{restricted_role_binding_name}' in namespace '{self.namespace}'")
            restricted_role_binding = {
                #'apiVersion': 'authorization.openshift.io/v1',
                #'kind': 'RoleBinding',
                'metadata': {
                    'namespace': self.namespace,
                    'name': restricted_role_binding_name
                },
                'roleRef': {
                    'kind': 'ClusterRole',
                    'name': 'restricted_' + role_binding['roleRef']['name']
                }
            }
            # role_binding.metadata.name = restricted_role_binding_name
            # role_binding.roleRef.name = 'restricted_' + role_binding.roleRef.name
            # role_binding.metadata.resourceVersion = None
            # role_binding.metadata.uid = None
            # role_binding.userNames = None
            restricted_role_binding['subjects'] = role_binding['subjects'] or []
            try:
                old_restricted_role_binding = self.v1_role_binding.get(namespace=self.namespace, name=restricted_role_binding_name).to_dict()
                old_restricted_subjects = old_restricted_role_binding['subjects'] or []
                restricted_role_binding['subjects'] += [subject for subject in old_restricted_subjects if subject not in role_binding['subjects']]
            except openshift.dynamic.exceptions.NotFoundError:
                pass
            #print(json.dumps(role_binding))
            self.v1_role_binding.apply(body=restricted_role_binding)
            self.v1_role_binding.delete(namespace=self.namespace, name=role_binding_name)


class RestrictedRoleOperator:
    def __init__(self):
        logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

        self.processed_resource_versions = {}


        # Disable SSL warnings: https://urllib3.readthedocs.io/en/latest/advanced-usage.html#ssl-warnings
        urllib3.disable_warnings()

        #if 'KUBERNETES_PORT' in os.environ:
        #    kubernetes.config.load_incluster_config()
        #else:
        #    kubernetes.config.load_kube_config()

        #configuration = kubernetes.client.Configuration()
        #kubernetes_client = kubernetes.client.api_client.ApiClient(configuration=configuration)
        # self.core_v1_api = kubernetes.client.CoreV1Api(api_client)
        # self.custom_objects_api = kubernetes.client.CustomObjectsApi(api_client)
        kubernetes_client = config.new_client_from_config()
        self.openshift_client = openshift.dynamic.DynamicClient(kubernetes_client)


    def run(self):
        v1_cluster_role = self.openshift_client.resources.get(group='rbac.authorization.k8s.io', api_version='v1', kind='ClusterRole')
        v1_role_binding = self.openshift_client.resources.get(group='rbac.authorization.k8s.io', api_version='v1', kind='RoleBinding')
        cluster_role_watcher = ClusterRoleWatcher(self.openshift_client)
        role_binding_watcher = RoleBindingWatcher(self.openshift_client, namespace='restricted-rule-operator')
        cluster_role_watcher.start()
        role_binding_watcher.start()


if __name__ == "__main__":
   signal.signal(signal.SIGUSR1, dumpstacks)
   operator = RestrictedRoleOperator()
   operator.run()

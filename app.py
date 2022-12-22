#!/usr/bin/env python3

import ctypes
import json
import logging
import os
import re
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
    def __init__(self, resource_client, namespace = None, label_selector = None): #, timeout = None):
        name = self.__class__.__name__
        if namespace:
            name += f"_{namespace}"
        super().__init__(name=name)

        self.resource_client = resource_client
        self.namespace = namespace
        self.label_selector = label_selector
        self._request_stop = False
        self.resource_version = None

    def run(self):
        # https://kubernetes.io/docs/reference/using-api/api-concepts/#efficient-detection-of-changes
        timeout = False
        while not self._request_stop:
            if not timeout:
                resources = self.resource_client.get(namespace=self.namespace, label_selector=self.label_selector).to_dict()
                self.reconcile_resources(resources)
                self.resource_version = resources['metadata'].get('resourceVersion')
                logging.info(f"Watching {self.resource_client.kind} in namespace '{self.namespace}' with resource version newer than '{self.resource_version}'")

            timeout = True
            for event in self.resource_client.watch(namespace=self.namespace, resource_version=self.resource_version, label_selector=self.label_selector, timeout=30):
                obj = event["raw_object"]
                operation = event['type']
                if operation == 'ERROR':
                    timeout = False
                    if obj.get('code') != 410:  # 410 is expected, meaning the watch expired
                        logging.error(event["raw_object"])
                        time.sleep(1)  # Prevent busy looping
                    break

                if obj['metadata'].get('resourceVersion'):
                    self.resource_version = obj['metadata']['resourceVersion']
                self.reconcile_resource(obj, operation)

        logging.info(f"Stop watching {self.resource_client.kind} in namespace '{self.namespace}'")

    def stop(self):
        self._request_stop = True
        #self._watcher.stop()

    def reconcile_resources(self, resources):
        for resource in resources['items']:
            self.reconcile_resource(resource, 'ADDED')


class ClusterRoleWatcher(AbstractWatcher):

    def __init__(self, openshift_client):
        self.v1_cluster_role = openshift_client.resources.get(group='rbac.authorization.k8s.io', api_version='v1', kind='ClusterRole')
        super().__init__(self.v1_cluster_role)

    def reconcile_resource(self, cluster_role, operation):
        if operation != 'DELETED' and cluster_role['metadata']['name'] in ('admin', 'edit'):
            cluster_role_name = cluster_role['metadata']['name']
            restricted_cluster_role_name = 'restricted_' + cluster_role_name

            logging.info(f"Reconciling ClusterRole '{restricted_cluster_role_name}'")

            #cluster_role.metadata.name = 'restricted_' + cluster_role.metadata.name
            restricted_rules = []
            for rule in cluster_role['rules']:
                if any(apiGroup in rule['apiGroups'] for apiGroup in ('route.openshift.io', 'networking.k8s.io')):
                    rule['verbs'][:] = [verb for verb in rule['verbs'] if verb in ('get', 'list', 'watch')]

                if rule['verbs']:
                    restricted_rules.append(rule)

            restricted_cluster_role = {
                'apiVersion': 'rbac.authorization.k8s.io/v1',
                'kind': 'ClusterRole',
                'metadata': {
                    'name': restricted_cluster_role_name
                },
                'rules': restricted_rules #[rule for rule in cluster_role['rules'] if not any(apiGroup in rule['apiGroups'] for apiGroup in ('route.openshift.io', 'networking.k8s.io'))]
            }

            self.v1_cluster_role.apply(body=restricted_cluster_role)


class RoleBindingWatcher(AbstractWatcher):

    def __init__(self, openshift_client, namespace):
        self.v1_role_binding = openshift_client.resources.get(group='rbac.authorization.k8s.io', api_version='v1', kind='RoleBinding')
        super().__init__(self.v1_role_binding, namespace=namespace)

    def reconcile_resource(self, role_binding, operation):
        if operation != 'DELETED' and not role_binding['roleRef'].get('namespace') and role_binding['roleRef']['name'] in ('admin', 'edit'):
            role_binding_name = role_binding['metadata']['name']
            restricted_role_binding_name = 'restricted_' + role_binding_name
            logging.info(f"Reconciling RoleBinding '{restricted_role_binding_name}' in namespace '{self.namespace}'")

            # Read current restricted subjects
            try:
                restricted_role_binding = self.v1_role_binding.get(namespace=self.namespace, name=restricted_role_binding_name).to_dict()
                restricted_subjects = restricted_role_binding['subjects'] or []
            except openshift.dynamic.exceptions.NotFoundError:
                restricted_subjects = []
                restricted_role_binding = {}

            subjects = []
            for subject in role_binding['subjects']:
                if subject not in restricted_subjects:
                    restricted_subjects.append(subject)

            if restricted_subjects:
                restricted_role_binding = {
                    'apiVersion': 'rbac.authorization.k8s.io/v1',
                    'kind': 'RoleBinding',
                    'metadata': {
                        'namespace': self.namespace,
                        'name': restricted_role_binding_name
                    },
                    'roleRef': {
                        'apiVersion': 'rbac.authorization.k8s.io/v1',
                        'kind': 'ClusterRole',
                        'name': 'restricted_' + role_binding['roleRef']['name']
                    },
                    'subjects': restricted_subjects,
                    'userNames': None
                }
                self.v1_role_binding.apply(body=restricted_role_binding)

            if subjects:
                role_binding = {
                    'apiVersion': 'rbac.authorization.k8s.io/v1',
                    'kind': 'RoleBinding',
                    'metadata': {
                        'namespace': self.namespace,
                        'name': role_binding_name
                    },
                    'roleRef': {
                        'apiVersion': 'rbac.authorization.k8s.io/v1',
                        'kind': 'ClusterRole',
                        'name': role_binding['roleRef']['name']
                    },
                    'subjects': subjects,
                    'userNames': None
                }
                self.v1_role_binding.apply(body=role_binding)
            else:
                self.v1_role_binding.delete(namespace=self.namespace, name=role_binding_name)


class ProjectWatcher(AbstractWatcher):

    def __init__(self, openshift_client, namespace_filter, label_selector=None):
        self.openshift_client = openshift_client
        self.namespace_filter = namespace_filter
        self.v1_project = openshift_client.resources.get(group='project.openshift.io', api_version='v1', kind='Project')
        super().__init__(self.v1_project, label_selector=label_selector)

    def reconcile_resource(self, project, operation):
        namespace = project['metadata']['name']
        if not self.namespace_filter.match(namespace):
            return

        watch = operation != 'DELETED' and project['metadata'].get('labels', {}).get('restricted-role-operator-exclude', "").lower() != 'true'
        watcher = [thread for thread in threading.enumerate() if thread.name == f"RoleBindingWatcher_{namespace}"]
        if watch and not watcher:
            watcher = RoleBindingWatcher(self.openshift_client, namespace=namespace)
            watcher.start()
        elif not watch and watcher:
            watcher[0].stop()
            watcher[0].join()



class RestrictedRoleOperator:
    def __init__(self):
        logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)

        # Disable SSL warnings: https://urllib3.readthedocs.io/en/latest/advanced-usage.html#ssl-warnings
        urllib3.disable_warnings()

        namespace_filter = os.getenv('NAMESPACE_FILTER')
        if namespace_filter:
            self.namespace_filter = re.compile(namespace_filter)
        else:
            raise RuntimeError("'NAMESPACE_FILTER' environment variable must be set to namespace filter regex!")

        if 'KUBERNETES_PORT' in os.environ:
           kubernetes.config.load_incluster_config()
        else:
           kubernetes.config.load_kube_config()

        configuration = kubernetes.client.Configuration()
        kubernetes_client = kubernetes.client.api_client.ApiClient(configuration=configuration)
        self.openshift_client = openshift.dynamic.DynamicClient(kubernetes_client)


    def run(self):
        # Disable for now
        # cluster_role_watcher = ClusterRoleWatcher(self.openshift_client)
        # cluster_role_watcher.start()

        project_watcher = ProjectWatcher(self.openshift_client, self.namespace_filter)
        project_watcher.start()



if __name__ == "__main__":
   signal.signal(signal.SIGUSR1, dumpstacks)
   operator = RestrictedRoleOperator()
   operator.run()

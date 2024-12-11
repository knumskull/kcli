import boto3
from kvirt.common import success, info2, pprint, error, fix_typos, warning
import os
import yaml
from yaml import safe_dump, safe_load

supported_versions = ['1.20', '1.21', '1.22', '1.23', '1.24', '1.25', '1.26', '1.27']


def project_init(config):
    access_key_id = config.options.get('access_key_id')
    access_key_secret = config.options.get('access_key_secret')
    session_token = config.options.get('session_token')
    region = config.options.get('region')
    return access_key_id, access_key_secret, session_token, region


def list_valid_roles(config, policy):
    results = {}
    access_key_id, access_key_secret, session_token, region = project_init(config)
    iam = boto3.client('iam', aws_access_key_id=access_key_id, aws_secret_access_key=access_key_secret,
                       region_name=region, aws_session_token=session_token)
    for role in iam.list_roles(MaxItems=1000)['Roles']:
        role_name = role['RoleName']
        for attached_policy in iam.list_attached_role_policies(RoleName=role_name)['AttachedPolicies']:
            if attached_policy['PolicyName'] == policy:
                results[role_name] = role['Arn']
                break
    return results


def get_kubeconfig(config, cluster, zonal=True):
    access_key_id, access_key_secret, session_token, region = project_init(config)
    eks = boto3.client('eks', aws_access_key_id=access_key_id, aws_secret_access_key=access_key_secret,
                       region_name=region, aws_session_token=session_token)
    cluster_data = eks.describe_cluster(name=cluster)
    cluster_cert = str(cluster_data["cluster"]["certificateAuthority"]["data"])
    cluster_ep = str(cluster_data["cluster"]["endpoint"])
    cluster_arn = cluster_data["cluster"]["arn"]
    cluster_config = {"apiVersion": "v1", "kind": "Config",
                      "clusters": [{"cluster": {"server": cluster_ep, "certificate-authority-data": cluster_cert},
                                    "name": cluster_arn}], "contexts": [{"context": {"cluster": cluster_arn,
                                                                                     "user": cluster_arn},
                                                                         "name": cluster_arn}],
                      "current-context": cluster_arn, "preferences": {},
                      "users": [{"name": cluster_arn, "user": {"exec": {
                          "apiVersion": "client.authentication.k8s.io/v1beta1", "command": "ekstoken",
                          "interactiveMode": "Never", "args": [config.client, cluster]}}}]}
    config_text = safe_dump(cluster_config, default_flow_style=False)
    clusterdir = os.path.expanduser(f"~/.kcli/clusters/{cluster}")
    with open(f"{clusterdir}/auth/kubeconfig", 'w') as f:
        f.write(config_text)


def scale(config, plandir, cluster, overrides):
    data = {'workers': 2,
            'network': 'default',
            'role': None,
            'disk_size': None,
            'flavor': None,
            'ami_type': None,
            'capacity_type': None,
            'version': None}
    data.update(overrides)
    cluster = overrides.get('cluster', cluster or 'myeks')
    version = data['version']
    workers = data['workers']
    disk_size = data['disk_size']
    flavor = data['flavor']
    ami_type = data['ami_type']
    capacity_type = data['capacity_type']
    access_key_id, access_key_secret, session_token, region = project_init(config)
    eks = boto3.client('eks', aws_access_key_id=access_key_id, aws_secret_access_key=access_key_secret,
                       region_name=region, aws_session_token=session_token)
    pprint(f"Updating nodegroup {cluster}")
    nodegroup_data = {'clusterName': cluster, 'nodegroupName': cluster,
                      'scalingConfig': {'minSize': workers, 'maxSize': 50, 'desiredSize': workers}}
    if version is not None:
        nodegroup_data['version'] = version
    if disk_size is not None:
        nodegroup_data['diskSize'] = disk_size
    if flavor is not None:
        nodegroup_data['instanceTypes'] = [flavor]
    if ami_type is not None:
        nodegroup_data['amiType'] = ami_type
    if capacity_type is not None:
        nodegroup_data['capacityType'] = capacity_type
    response = eks.update_nodegroup_config(**nodegroup_data)
    if config.debug:
        print(response)
    return {'result': 'success'}


def create(config, plandir, cluster, overrides, dnsconfig=None):
    data = safe_load(open(f'{plandir}/kcli_default.yml'))
    data.update(overrides)
    fix_typos(data)
    k = config.k
    version = data['version']
    workers = data['workers']
    ctlplane_role = data['ctlplane_role']
    worker_role = data['worker_role']
    disk_size = data['disk_size']
    flavor = data['flavor']
    ami_type = data['ami_type']
    capacity_type = data['capacity_type']
    network = data['subnet'] or data['network']
    extra_networks = data['extra_subnets'] or data['extra_networks']
    sgid = data['security_group']
    plan = cluster
    tags = {'plan': cluster, 'kube': cluster, 'kubetype': 'eks'}
    cluster_data = {'name': cluster, 'tags': tags}
    if not data['default_addons']:
        warning("Disabling network add-ons")
        cluster_data['bootstrapSelfManagedAddons'] = True
    else:
        auto_mode = {'storageConfig': {'blockStorage': {'enabled': True}},
                     'kubernetesNetworkConfig': {'elasticLoadBalancing': {'enabled': True}},
                     'computeConfig': {'enabled': True}}
        cluster_data.update(auto_mode)
    extended_support = data['extended_support']
    if not extended_support:
        cluster_data['upgradePolicy'] = {'supportType': 'STANDARD'}
    zonal_shift = data['zonal_shift']
    if zonal_shift:
        cluster_data['zonalShiftConfig'] = {'enabled': True}
    logging = data['logging']
    if logging:
        logging_data = []
        for _type in data['logging_types']:
            logging_data.append({'type': _type, 'enabled': True})
        cluster_data['logging'] = {'clusterLogging': logging_data}
    if version is not None:
        version = str(version)
        if version not in supported_versions:
            msg = f"Version needs to be one of those: {(',').join(supported_versions)}"
            return {'result': 'failure', 'reason': msg}
        cluster_data['version'] = version
    clusterdir = os.path.expanduser(f"~/.kcli/clusters/{cluster}")
    if os.path.exists(clusterdir):
        return {'result': 'failure', 'reason': f"Remove existing directory {clusterdir} or use --force"}
    else:
        os.makedirs(clusterdir)
        os.mkdir(f"{clusterdir}/auth")
        with open(f"{clusterdir}/kcli_parameters.yml", 'w') as p:
            installparam = overrides.copy()
            installparam['plan'] = plan
            installparam['cluster'] = cluster
            installparam['kubetype'] = 'eks'
            installparam['client'] = config.client
            yaml.safe_dump(installparam, p, default_flow_style=False, encoding='utf-8', allow_unicode=True)
    access_key_id, access_key_secret, session_token, region = project_init(config)
    ctlplane_roles = list_valid_roles(config, 'AmazonEKSClusterPolicy')
    if ctlplane_role is not None:
        if ctlplane_role not in ctlplane_roles:
            return {'result': 'failure', 'reason': f"Invalid role {ctlplane_role}"}
    elif not ctlplane_roles:
        return {'result': 'failure', 'reason': "No role with AmazonEKSClusterPolicy found"}
    else:
        ctlplane_role = [*ctlplane_roles][0]
        pprint(f"Using ctlplane role {ctlplane_role}")
    ctlplane_role = ctlplane_roles[ctlplane_role]
    cluster_data['roleArn'] = ctlplane_role
    worker_roles = list_valid_roles(config, 'AmazonEKSWorkerNodePolicy')
    if worker_role is not None:
        if worker_role not in worker_roles:
            return {'result': 'failure', 'reason': f"Invalid role {worker_role}"}
    elif not worker_roles:
        return {'result': 'failure', 'reason': "No role with AmazonEKSWorkerNodePolicy found"}
    else:
        worker_role = [*worker_roles][0]
        pprint(f"Using worker role {worker_role}")
    worker_role = worker_roles[worker_role]
    subnetids = []
    total_subnets = [network] + extra_networks
    for index, n in enumerate(total_subnets):
        vpcid, subnetid, az = k.eks_get_network(n)
        if index == 0:
            sgid = k.get_security_group_id(n, vpcid) if sgid is not None else k.get_default_security_group_id(vpcid)
            if sgid is None:
                return {'result': 'failure', 'reason': "Couldn't find a valid security group"}
        subnetids.append(subnetid)
    if len(total_subnets) == 1:
        subnets = k.list_subnets()
        subnetid = None
        for subnetname in subnets:
            subnet = subnets[subnetname]
            if subnet['network'] == vpcid and subnet['id'] != subnetid and subnet['az'] != az:
                subnetid = subnet['id']
                break
        if subnetid is None:
            return {'result': 'failure', 'reason': "Couldn't find a valid subnet in the same vpc but with other az"}
        else:
            pprint(f"Using subnet {subnetid} as extra subnet")
            subnetids.append(subnetid)
    cluster_data['resourcesVpcConfig'] = {'subnetIds': subnetids, 'securityGroupIds': [sgid]}
    eks = boto3.client('eks', aws_access_key_id=access_key_id, aws_secret_access_key=access_key_secret,
                       region_name=region, aws_session_token=session_token)
    pprint(f"Creating cluster {cluster}")
    response = eks.create_cluster(**cluster_data)
    if config.debug:
        print(response)
    pprint("Waiting for cluster to be created")
    waiter = eks.get_waiter("cluster_active")
    waiter.wait(name=cluster)
    get_kubeconfig(config, cluster)
    nodegroup_data = {'clusterName': cluster, 'nodegroupName': cluster, 'scalingConfig':
                      {'minSize': workers, 'maxSize': 50, 'desiredSize': workers}, 'subnets': subnetids, 'tags': tags,
                      'nodeRole': worker_role}
    keypair = config.options.get('keypair')
    if keypair is not None:
        nodegroup_data['remoteAccess'] = {'ec2SshKey': keypair, 'sourceSecurityGroups': [sgid]}
    if version is not None:
        nodegroup_data['version'] = version
    if disk_size is not None:
        nodegroup_data['diskSize'] = disk_size
    if flavor is not None:
        nodegroup_data['instanceTypes'] = [flavor]
    if ami_type is not None:
        nodegroup_data['amiType'] = ami_type
    if capacity_type is not None:
        nodegroup_data['capacityType'] = capacity_type
    pprint(f"Creating nodegroup {cluster}")
    response = eks.create_nodegroup(**nodegroup_data)
    if config.debug:
        print(response)
    waiter = eks.get_waiter("cluster_active")
    waiter.wait(name=cluster)
    success(f"Kubernetes cluster {cluster} deployed!!!")
    info2(f"export KUBECONFIG=$HOME/.kcli/clusters/{cluster}/auth/kubeconfig")
    info2("export PATH=$PWD:$PATH")
    return {'result': 'success'}


def delete(config, cluster, zonal=True):
    fail = False
    access_key_id, access_key_secret, session_token, region = project_init(config)
    eks = boto3.client('eks', aws_access_key_id=access_key_id, aws_secret_access_key=access_key_secret,
                       region_name=region, aws_session_token=session_token)
    try:
        response = eks.delete_nodegroup(clusterName=cluster, nodegroupName=cluster)
        if config.debug:
            print(response)
        pprint("Waiting for nodegroup to be deleted")
        waiter = eks.get_waiter("nodegroup_deleted")
        waiter.wait(clusterName=cluster, nodegroupName=cluster)
    except Exception as e:
        fail = True
        error(f"Hit Issue when getting {cluster}: {e}")
    try:
        response = eks.delete_cluster(name=cluster)
        if config.debug:
            print(response)
        pprint("Waiting for cluster to be deleted")
        waiter = eks.get_waiter("cluster_deleted")
        waiter.wait(name=cluster)
    except Exception as e:
        fail = True
        error(f"Hit Issue when getting {cluster}: {e}")
    if fail:
        return {'result': 'failure', 'reason': 'Hit issue'}
    else:
        return {'result': 'success'}


def list(config):
    results = {}
    access_key_id, access_key_secret, session_token, region = project_init(config)
    eks = boto3.client('eks', aws_access_key_id=access_key_id, aws_secret_access_key=access_key_secret,
                       region_name=region, aws_session_token=session_token)
    for cluster in eks.list_clusters()['clusters']:
        results[cluster] = {'type': 'eks', 'plan': None, 'vms': []}
    return results


def info(config, cluster, debug=False):
    results = {}
    access_key_id, access_key_secret, session_token, region = project_init(config)
    eks = boto3.client('eks', aws_access_key_id=access_key_id, aws_secret_access_key=access_key_secret,
                       region_name=region, aws_session_token=session_token)
    try:
        response = eks.describe_cluster(name=cluster)['cluster']
        if debug:
            print(response)
    except Exception as e:
        error(e)
        return {}
    results = {'nodes': [], 'version': response['version']}
    return results


def info_service(config, zonal=True):
    return {}

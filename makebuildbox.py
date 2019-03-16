#!/usr/bin/env python
# pylint: disable=C

import click
import googleapiclient.discovery
import googleapiclient.errors
import os, time, copy
import yaml

compute = googleapiclient.discovery.build("compute", "v1")
dns = googleapiclient.discovery.build("dns", "v1")

zone = ""
region = ""
project = ""
disk = ""
managedZone = ""
frontendDNS = ""
instanceGroup = ""
instancename = ""


def read_sibling(fn, as_yaml=False):
    with open(os.path.join(os.path.dirname(__file__), "yamls", fn)) as f:
        if as_yaml:
            return yaml.safe_load(f)
        return f.read()

config = read_sibling("config.priv.yml", as_yaml=True)
for k, v in config:
    globals()[k] = v

def wait_for_operation(operation):
    print("Waiting for operation to finish...")
    while True:
        result = (
            compute.zoneOperations()
            .get(project=project, zone=zone, operation=operation)
            .execute()
        )

        if result["status"] == "DONE":
            print("done.")
            if "error" in result:
                raise Exception(result["error"])
            return result

        time.sleep(1)


@click.group()
def cli():
    pass


@cli.add_command
@click.command()
@click.argument("cpus", type=int, default=1)
def spawn(cpus):

    click.echo("creating target-proxy")
    body = {
        "name": "dev-target-proxy",
        "protocol": "HTTPS",
        "quicOverride": "NONE",
        "sslCertificates": [f"projects/{project}/global/sslCertificates/dev"],
        "urlMap": f"projects/{project}/global/urlMaps/dev",
    }
    try:
        compute.targetHttpsProxies().insert(project=project, body=body).execute()
    except googleapiclient.errors.HttpError as e:
        if e.resp.status != 409:
            raise
    time.sleep(2)
    click.echo("creating forwarding rule")
    body = {
        "IPProtocol": "TCP",
        "ipVersion": "IPV4",
        "loadBalancingScheme": "EXTERNAL",
        "name": "dev",
        "networkTier": "PREMIUM",
        "portRange": "443-443",
        "target": f"projects/{project}/global/targetHttpsProxies/dev-target-proxy",
    }
    try:
        compute.globalForwardingRules().insert(project=project, body=body).execute()
    except googleapiclient.errors.HttpError as e:
        if e.resp.status != 409:
            raise

    click.echo("creating VM")

    image_response = (
        compute.images()
        .getFromFamily(project="cos-cloud", family="cos-stable")
        .execute()
    )
    source_disk_image = image_response["selfLink"]

    config = {
        "name": instancename,
        "machineType": f"zones/{zone}/machineTypes/n1-standard-{cpus}",
        # Specify the boot disk and the image to use as a source.
        "disks": [
            {
                "boot": True,
                "autoDelete": True,
                "initializeParams": {"sourceImage": source_disk_image},
            },
            {
                "boot": False,
                "autoDelete": False,
                "source": f"projects/{project}/zones/{zone}/disks/{disk}",
            },
        ],
        # Specify a network interface with NAT to access the public
        # internet.
        "networkInterfaces": [
            {
                "network": "global/networks/default",
                "accessConfigs": [{"type": "ONE_TO_ONE_NAT", "name": "External NAT"}],
            }
        ],
        # # Allow the instance to access cloud storage and logging.
        # 'serviceAccounts': [{
        #     'email': 'default',
        #     'scopes': [
        #         'https://www.googleapis.com/auth/devstorage.read_write',
        #         'https://www.googleapis.com/auth/logging.write'
        #     ]
        # }],
        # Metadata is readable from the instance and allows you to
        # pass configuration from deployment scripts to instances.
        "metadata": {
            "items": [
                {
                    # cloud init script
                    "key": "user-data",
                    "value": read_sibling("cloud_init.yml"),
                },
                {
                    # container spec
                    "key": "gce-container-declaration",
                    "value": read_sibling("containers.yml"),
                },
            ]
        },
    }

    op = compute.instances().insert(project=project, zone=zone, body=config).execute()
    wait_for_operation(op["name"])

    config = {
        "instances": [
            {
                "instance": f"https://www.googleapis.com/compute/v1/projects/{project}/zones/{zone}/instances/{instancename}"
            }
        ]
    }
    compute.instanceGroups().addInstances(
        project=project, zone=zone, instanceGroup=instanceGroup, body=config
    ).execute()


    while True:
        res = (
            compute.globalForwardingRules()
            .get(project=project, forwardingRule="dev")
            .execute()
        )
        if res.get("IPAddress"):
            break
        time.sleep(1)

    IPAddress = res["IPAddress"]
    click.echo(f"configure DNS to {IPAddress}")
    res = (
        dns.resourceRecordSets()
        .list(project=project, managedZone=managedZone)
        .execute()
    )
    for i in res.get("rrsets", []):
        if i["name"] == frontendDNS:
            n = copy.copy(i)
            n["rrdatas"] = [IPAddress]
            body = {"additions": [n], "deletions": [i]}
            res = (
                dns.changes()
                .create(project=project, managedZone=managedZone, body=body)
                .execute()
            )


@cli.add_command
@click.command()
def cleanup():
    click.echo("cleanup instances")
    result = compute.instances().list(project=project, zone=zone).execute()
    for i in result.get("items", []):
        compute.instances().delete(
            project=project, zone=zone, instance=i["name"]
        ).execute()
    click.echo("cleanup globalForwardingRules")
    result = compute.globalForwardingRules().list(project=project).execute()
    for i in result.get("items", []):
        compute.globalForwardingRules().delete(
            project=project, forwardingRule=i["name"]
        ).execute()
    while True:
        res = compute.globalForwardingRules().list(project=project).execute()
        if not res.get("items"):
            break
        time.sleep(1)
    click.echo("cleanup targetHttpsProxies")
    result = compute.targetHttpsProxies().list(project=project).execute()
    print(result)
    for i in result.get("items", []):
        compute.targetHttpsProxies().delete(
            project=project, targetHttpsProxy=i["name"]
        ).execute()
    while True:
        res = compute.globalForwardingRules().list(project=project).execute()
        if not res.get("items"):
            break
        time.sleep(1)


if __name__ == "__main__":
    cli()

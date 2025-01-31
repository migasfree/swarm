#!/usr/bin/python3

# https://docker-py.readthedocs.io/en/stable/

import os
import docker
import string
import random
import subprocess
import requests
import time
import socket
import urllib3
import shutil

from template import render
from portainer import PortainerAPI, create_token
from context import ContextLoader, get_stacks

from cryptography import x509
from cryptography.hazmat.backends import default_backend
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


_PATH = "/stack"  # Path in this container
_PATH_SHARE = "/mnt/cluster"  # data shared
_PATH_CREDENTIALS = os.path.join(_PATH_SHARE, "credentials")
_PATH_CERTIFICATE = os.path.join(_PATH_SHARE, "certificates")
_FILE_SETTINGS = os.path.join(_PATH, "settings.py")


# FUNCTIONS
# =========
def is_self_signed(certificate_path):
    with open(certificate_path, "rb") as cert_file:
        cert_data = cert_file.read()
    cert = x509.load_pem_x509_certificate(cert_data, default_backend())
    return 'CN=Insecure Certificate Authority' in str(cert.issuer)

def resolver_dns(domain):
    while True:
        try:
            return socket.gethostbyname(domain)
        except Exception:
            time.sleep(2)
            print(f"Error resolving domain name: {domain}")


def swarm_init():
    info = client.info()

    if 'Swarm' in info and 'Cluster' in info['Swarm']:
        cluster_info = info['Swarm']['Cluster']
        cluster_id = cluster_info['ID']

    if 'cluster_id' not in locals():
        print()
        print("Warning! This system is not a Swarm node.")
        response = "Y"
        response = input("Do you want to create a manager node? (Y/n): ") or response
        if response.upper() == "Y":
            try:
                cluster_id = client.swarm.init()
            except docker.errors.APIError as e:
                print(e)
                if "could not choose an IP address to advertise" in str(e):
                    advertise_addr = input("Please input the IP address to advertise: ")
                    try:
                        cluster_id = client.swarm.init(advertise_addr=advertise_addr)
                    except docker.errors.APIError as e:
                        print("Error: cluster not initiate", e)
                        return None
                else:
                    print("Error: cluster not initiate", e)
                    return None
    return cluster_id


def generate_password(length):
    valid_characters = string.ascii_letters + string.digits
    password = ''.join(random.choice(valid_characters) for _ in range(length))
    return password


def wait_for_service(service_name, timeout=30):
    start_time = time.time()
    print(f"waiting {service_name} ", end="", flush=True)
    while True:
        if time.time() - start_time > timeout:
            raise TimeoutError(f"Timed out waiting for service '{service_name}' to start")
        print(".", end="", flush=True)
        service = client.services.get(service_name)
        for task in service.tasks():
            if task["Status"]["State"] == "running":
                print()
                if service_name == "portainer_portainer":
                    proxy_reconfigure()
                return

        time.sleep(2)


def download_resource(url, output_path):
    response = requests.get(url)
    if response.status_code == 200:
        with open(output_path, 'wb') as f:
            f.write(response.content)
        print(f"Archivo descargado correctamente en {output_path}")
    else:
        print(f"Error al descargar el archivo: {response.status_code}")


def create_labels():
    nodes = client.nodes.list()
    # if only one node
    if len(nodes) == 1:
        node = nodes[0]

        # Add labels to the node
        labels = {
            # "datashare": "true", is only for s3
            "datastore": "true",
            "database": "true"
        }

        node_spec = {
            'Availability': 'active',
            'Name': 'node-1',
            'Role': 'manager',
            'Labels': labels
        }

        node.update(node_spec)


def deploy_proxy(context):
    path_template = "/tools/templates/"
    template = "proxy.template"
    deploy = os.path.join(_PATH, template)
    with open(deploy, 'w') as file_deploy:
        file_deploy.write(render(path_template, template, context))

    # Secrets swarm-credential
    credentials("swarm-credential", generate_password(8))
    create_secret_file("swarm-credential", os.path.join(_PATH_CREDENTIALS, "swarm-credential"))

    deploy_stack(deploy, "proxy")
    wait_for_service("proxy_proxy", 300)
    print()
    print(f"● https://{context['FQDN']}/services/status")
    print()
    os.remove(deploy)


def proxy_reconfigure():
    try:
        requests.post("http://proxy:8001/services/reconfigure")
    except:
        pass


def deploy_portainer(context):

    # Deploy Portainer if it doesn't exist
    if "portainer" not in [stack.name for stack in client.services.list()]:

        # Render portainer.template
        # template_file = "/tools/templates/portainer.template"
        file_yml = "/stack/portainer.yml"
        # CUSTOM TEMPLATE
        # name_template = f"{context['STACK']}"
        content = render("/tools/templates", "portainer.template", context)
        with open(file_yml, "w") as f:
            f.write(content)

        # Secrets portainer
        # credentials("swarm-credential", generate_password(8))

        deploy_stack(file_yml, "portainer")
        os.remove(file_yml)
        wait_for_service("portainer_portainer", 300)
        time.sleep(3)

        # credentials configuration
        (user, password) = credentials("swarm-credential")

        # print("IP PORTAINER", resolver_dns('portainer'))
        # resolver_dns('portainer')

        response = requests.post(
            "http://portainer:9000/api/users/admin/init",
            json={"Username": user, "Password": password},
            verify=False
        )
        if response and response.status_code != 200:
            print("RESPONSE INIT", response)
            print("RESPONSE INIT", response.text)

        response = requests.get('http://portainer:9000/#!/wizard', verify=False)
        if response and response.status_code != 200:
            print("RESPONSE WIZARD", response)

        if os.path.exists(f"{_PATH_CREDENTIALS}/portainer-token"):
            token = open(f"{_PATH_CREDENTIALS}/portainer-token", "r").read()
        else:
            token = create_token("deploy", user, password)
            open(f"{_PATH_CREDENTIALS}/portainer-token", "w").write(token)

        if token == "":
            print("Algo salió mal. Borra credentials/portainer-token, por favor.")
            exit()

        # Customize logo
        api = PortainerAPI("http://portainer:9000/api", token)
        api.settings()

        # Create Environment
        # api.create_environment("migasfree cluster (swarm)")
        api.set_enpoint_id("primary")

        # Update Public IP
        api.set_public_ip(context['FQDN'])

        print()
        print(f"● https://portainer.{context['FQDN']}/ ")
        print()


def credentials(credential_name, user="admin"):
    """
    Save & Read credentials with 'user:password' content
    """
    filename = os.path.join(_PATH_CREDENTIALS, credential_name)
    # if not exist, create it
    if not os.path.exists(filename):
        with open(filename, "w") as credential_file:
            credential_file.write(f"{user}:{generate_password(30)}")

    user, password = open(f"{_PATH_CREDENTIALS}/{credential_name}").read().split(":")
    return (user, password)


def create_secret_file(name, file_path):
    existing_secrets = [secret.name for secret in client.secrets.list()]
    if name not in existing_secrets:
        with open(file_path, 'rb') as f:
            data = f.read()
        create_secret(name=name, data=data)


def create_secret(name, data):
    existing_secrets = [secret.name for secret in client.secrets.list()]
    if name not in existing_secrets:
        client.secrets.create(name=name, data=data)


def deploy_stack(compose_file, stack_name):
    os.system(f'docker stack deploy -c {compose_file} {stack_name} --detach=true --resolve-image=never')


def create_network_overlay(network_name):
    os.system(f'docker network create --attachable --driver overlay --opt encrypted {network_name} 2>/dev/null')

def create_network_internal(network_name):
    os.system(f'docker network create --internal --driver overlay --opt encrypted {network_name} 2>/dev/null')


def deploy_migasfree(context):
    print()
    print(f"● https://{context['FQDN']}/services/status")
    print()

    print(f"Deploying the '{context['STACK']}' stack. Please wait.")

    create_network_internal(f"{context['STACK']}_network")

    token = open(f"{_PATH_CREDENTIALS}/portainer-token", "r").read()
    wait_for_service("portainer_portainer", 300)
    api = PortainerAPI("http://portainer:9000/api", token)
    file_yml = f"/stack/{context['STACK']}.yml"
    api.set_enpoint_id("primary")

    """
    # Secrets stack
    (user, password) = credentials(f"{CONTEXT['STACK']}","admin")

    # Stack secrets
    api.create_secret(f"{context['STACK']}_superadmin_name", user)
    api.create_secret(f"{context['STACK']}_superadmin_pass", password)
    api.create_secret(f"{context['STACK']}_pms_pass", generate_password(12))
    """

    # CUSTOM TEMPLATE
    name_template = f"{context['STACK']}"

    api.delete_custom_templates(name_template)

    content = render("/tools/templates", "stack.template", context)
    with open(file_yml, "w") as f:
        f.write(content)

    payload = {
        "Title": name_template,
        "FileContent": content,
        "File": None,
        "RepositoryURL": "",
        "RepositoryReferenceName": "",
        "RepositoryAuthentication": False,
        "RepositoryUsername": "",
        "RepositoryPassword": "",
        "ComposeFilePathInRepository": f"{context['STACK']}.yml",
        "Description": "migasfree stack",
        "Note": "http://migasfree.org",
        "Logo": "https://raw.githubusercontent.com/migasfree/migasfree-frontend/master/public/favicon.svg",
        "Platform": 1,
        "Type": 1,
        "AccessControlData": {
            "AccessControlEnabled": True,
            "Ownership": "administrators",
            "AuthorizedUsers": [],
            "AuthorizedTeams": []
        },
        "Variables": [],
        "TLSSkipVerify": False
    }
    api.custom_templates(payload)

    # DEPLOY THE STACK
    payload = {
        "Env": [],
        "Name":	name_template,
        "StackFileContent": content,
        "SwarmID": api.swarm_id
    }

    # api.deploy(payload)
    deploy_stack(file_yml, f"{context['STACK']}")

    os.remove(file_yml)

    print()


def create_paths():
    if not os.path.exists(_PATH_CREDENTIALS):
        os.mkdir(_PATH_CREDENTIALS)
    if not os.path.exists(_PATH_CERTIFICATE):
        os.mkdir(_PATH_CERTIFICATE)
    if not os.path.exists(f"{_PATH_SHARE}/datashares/"):
        os.mkdir(f"{_PATH_SHARE}/datashares/")
        os.chown(f"{_PATH_SHARE}/datashares/", 890, 890)
    if not os.path.exists(f"{_PATH_SHARE}/datashares/{CONTEXT['STACK']}"):
        os.mkdir(f"{_PATH_SHARE}/datashares/{CONTEXT['STACK']}")
        os.chown(f"{_PATH_SHARE}/datashares/{CONTEXT['STACK']}", 890, 890)


# PROGRAM
# =======

cl = ContextLoader()
CONTEXT = cl.context
cl.save()

cl.load_stack(" | ".join(get_stacks()))
CONTEXT = cl.context
cl.save_stack()

create_paths()

client = docker.from_env()
swarm_init()

if CONTEXT["HTTPSMODE"] == 'manual':
    subprocess.run(['sh', '/usr/bin/self-certificate.sh', CONTEXT['FQDN'], CONTEXT['STACK']])

(user, password) = credentials(f"{CONTEXT['STACK']}", "admin")

# Stack secrets
create_secret(f"{CONTEXT['STACK']}_superadmin_name", user)
create_secret(f"{CONTEXT['STACK']}_superadmin_pass", password)
create_secret(f"{CONTEXT['STACK']}_pms_pass", generate_password(12))

create_labels()
create_network_overlay("proxy")

# Connect network portainer to this container (is Necessary in credential configuration)
client.networks.get("proxy").connect(socket.gethostname())

deploy_proxy(CONTEXT)

deploy_portainer(CONTEXT)
proxy_reconfigure()

deploy_migasfree(CONTEXT)

if CONTEXT["HTTPSMODE"] == 'auto' and is_self_signed(f"{_PATH_CERTIFICATE}/{CONTEXT['STACK']}.pem"):
    print("Changing to HTTPSMODE auto")
    wait_for_service(f"{CONTEXT['STACK']}_certbot")
    token = open(f"{_PATH_CREDENTIALS}/portainer-token", "r").read()
    api = PortainerAPI("http://portainer:9000/api", token)
    api.set_enpoint_id("primary")
    api.execute_in_service(f"{CONTEXT['STACK']}_certbot", ["/usr/bin/send_message","HTTPSMODE='auto'"])
    api.execute_in_service(f"{CONTEXT['STACK']}_certbot", ["/usr/bin/renew-certificates.sh"])
    api.execute_in_service(f"{CONTEXT['STACK']}_certbot", ["/usr/bin/send_message",""])
    api.execute_in_service(f"proxy_proxy", ["/usr/bin/reload"])

try:
    shutil.rmtree(f"/mnt/cluster/datashares/{CONTEXT['STACK']}/__pycache__")
except Exception:
    pass

import configparser
import os
import time
import boto3
from botocore.exceptions import ClientError

ROOT = os.path.dirname(__file__)
AWS_CREDS_PATH = os.path.join(ROOT, '.awscreds')
if not os.path.exists(AWS_CREDS_PATH):
    root_creds = os.path.join(os.path.dirname(ROOT), '.awscreds')
    if os.path.exists(root_creds):
        AWS_CREDS_PATH = root_creds

if not os.path.exists(AWS_CREDS_PATH):
    raise FileNotFoundError(f"AWS creds file not found at {AWS_CREDS_PATH}")

config = configparser.ConfigParser()
config.read(AWS_CREDS_PATH)
if not config.sections():
    raise RuntimeError('No sections found in AWS creds file')
section = config.sections()[0]
creds = config[section]

session = boto3.Session(
    aws_access_key_id=creds.get('aws_access_key_id'),
    aws_secret_access_key=creds.get('aws_secret_access_key'),
    aws_session_token=creds.get('aws_session_token'),
    region_name=creds.get('aws_region'),
)

ec2 = session.resource('ec2')
client = session.client('ec2')
ssm = session.client('ssm')

print('Using AWS region:', creds.get('aws_region'))

# Resolve latest Amazon Linux 2 AMI via SSM parameter store.
ami_param = '/aws/service/ami-amazon-linux-latest/amzn2-ami-hvm-x86_64-gp2'
print('Resolving latest Amazon Linux 2 AMI...')
ami = ssm.get_parameter(Name=ami_param)['Parameter']['Value']
print('AMI resolved:', ami)

key_name = 'navigator-gateway-key'
key_path = os.path.join(ROOT, f'{key_name}.pem')

sg_name = 'navigator-gateway-sg'
try:
    existing = list(ec2.security_groups.filter(Filters=[
        {'Name': 'group-name', 'Values': [sg_name]},
    ]))
except ClientError as exc:
    raise RuntimeError('Failed to query security groups: ' + str(exc))

if existing:
    sg = existing[0]
    print('Reusing security group:', sg.group_id)
else:
    print('Creating security group:', sg_name)
    vpcs = list(ec2.vpcs.limit(1))
    if not vpcs:
        raise RuntimeError('No VPCs available in this region')
    vpc = vpcs[0]
    sg = ec2.create_security_group(
        GroupName=sg_name,
        Description='Navigator gateway relay security group',
        VpcId=vpc.id,
    )
    print('Created SG:', sg.group_id)

required_rules = [
    {'IpProtocol': 'tcp', 'FromPort': 59020, 'ToPort': 59020, 'IpRanges': [{'CidrIp': '0.0.0.0/0', 'Description': 'Navigator gateway relay'}]},
    {'IpProtocol': 'tcp', 'FromPort': 22, 'ToPort': 22, 'IpRanges': [{'CidrIp': '0.0.0.0/0', 'Description': 'SSH access for gateway host'}]},
]

existing_ports = {(perm.get('FromPort'), perm.get('ToPort')) for perm in sg.ip_permissions}
for rule in required_rules:
    if (rule['FromPort'], rule['ToPort']) not in existing_ports:
        print(f'Adding missing inbound rule for {rule["FromPort"]}')
        sg.authorize_ingress(IpPermissions=[rule])

if not os.path.exists(key_path):
    print('Creating EC2 key pair:', key_name)
    try:
        key_pair = client.create_key_pair(KeyName=key_name)
    except ClientError as exc:
        if exc.response.get('Error', {}).get('Code') == 'InvalidKeyPair.Duplicate':
            print('Key pair already exists in AWS; make sure local key file is available at', key_path)
            key_pair = None
        else:
            raise
    if key_pair is not None:
        with open(key_path, 'w', encoding='utf-8') as key_file:
            key_file.write(key_pair['KeyMaterial'])
        try:
            os.chmod(key_path, 0o600)
        except OSError:
            pass
        print('Saved private key to', key_path)
else:
    print('Using existing local key file:', key_path)

PROJECT_ROOT = os.path.dirname(ROOT)

# Read gateway deployment script files
with open(os.path.join(PROJECT_ROOT, 'aws_gateway.py'), 'r', encoding='utf-8') as f:
    gateway_code = f.read()
with open(os.path.join(PROJECT_ROOT, 'common.py'), 'r', encoding='utf-8') as f:
    common_code = f.read()

# Build userdata to install Python and run gateway service.
user_data = f"""#!/bin/bash

yum update -y
yum install -y python3

cat > /home/ec2-user/aws_gateway.py <<'NAVIGATOR_PY'
{gateway_code}
NAVIGATOR_PY

cat > /home/ec2-user/common.py <<'NAVIGATOR_COMMON'
{common_code}
NAVIGATOR_COMMON

cat > /etc/systemd/system/navigator-gateway.service <<'NAVIGATOR_SERVICE'
[Unit]
Description=Navigator Gateway Relay
After=network.target

[Service]
Type=simple
WorkingDirectory=/home/ec2-user
ExecStart=/usr/bin/python3 /home/ec2-user/aws_gateway.py --host 0.0.0.0 --port 59020
Restart=always
User=ec2-user
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
NAVIGATOR_SERVICE

chmod 644 /etc/systemd/system/navigator-gateway.service
chown ec2-user:ec2-user /home/ec2-user/aws_gateway.py /home/ec2-user/common.py
systemctl daemon-reload
systemctl enable navigator-gateway.service
systemctl start navigator-gateway.service
"""

print('Launching EC2 instance...')

instance_name = 'navigator-gateway-host-2'
create_args = {
    'ImageId': ami,
    'InstanceType': 't3.micro',
    'MinCount': 1,
    'MaxCount': 1,
    'SecurityGroupIds': [sg.group_id],
    'UserData': user_data,
    'TagSpecifications': [
        {
            'ResourceType': 'instance',
            'Tags': [
                {'Key': 'Name', 'Value': instance_name},
                {'Key': 'Project', 'Value': 'navigator-devs'},
            ],
        },
    ],
}
if os.path.exists(key_path):
    create_args['KeyName'] = key_name

instances = ec2.create_instances(**create_args)
instance = instances[0]
print('Instance created:', instance.id)
print('Waiting for instance to run...')
instance.wait_until_running()
instance.reload()
print('Instance state:', instance.state['Name'])
print('Public DNS:', instance.public_dns_name)
print('Public IP:', instance.public_ip_address)
print('Security Group:', sg.group_id)
if os.path.exists(key_path):
    print('SSH key saved to:', key_path)
    print('Use: ssh -i', key_path, 'ec2-user@' + instance.public_dns_name)
print('To connect local server use this gateway host and port 59020')

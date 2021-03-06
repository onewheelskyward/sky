import re
import time
import random
import logging
from operator import itemgetter
import boto
from .networking import connect_vpc, create_route_table
from .state import config, mode

logger = logging.getLogger(__name__)

def connect_ec2():
    """
    Connect to the Amazon Elastic Compute Cloud (Amazon EC2) service.
    """
    logger.debug('Connecting to the Amazon Elastic Compute Cloud (Amazon EC2) service.')
    ec2 = boto.connect_ec2(aws_access_key_id=config['AWS_ACCESS_KEY_ID'],
                           aws_secret_access_key=config['AWS_SECRET_ACCESS_KEY'])
    logger.debug('Connected to Amazon EC2.')

    return ec2

def create_security_group(vpc, name=None, database_backend=None, allowed_inbound_traffic=[], allowed_outbound_traffic=[]):
    """
    Create an Amazon EC2-VPC Security Group.

    :type name: str
    :param name: An *optional* name for the EC2-VPC security group. A name will
        be generated from the current project name, if one is not specified.

    :type database_backend: string
    :param database_backend: An *optional* database backend. This is intended to
        be used with application security groups that require outbound traffic
        to a database.

        * Supported database backends:

            * ``postgresql``
            * ``mysql``
            * ``oracle``

    :type allowed_inbound_traffic: list
    :param allowed_inbound_traffic: A list of tuples in the format:
        ``(protocol, cidr_block)`` or ``(protocol, security_group)``.

        * **protocol** (*string*):

            * ``HTTP``
            * ``HTTPS``
            * ``TCP:Port`` (e.g., ``TCP:80``)
            * ``UDP:Port`` (e.g., ``UDP:1337``)

        * **cidr_block** (*string*):

            * A range of IP addresses from which inbound traffic is allowed.

        * **security_group** (:class:`~boto.ec2.securitygroup.SecurityGroup`)

            * A :class:`boto.ec2.securitygroup.SecurityGroup` that is allowed ingress into the created security group.

    :type allowed_outbound_traffic: list
    :param allowed_outbound_traffic: A list of tuples in the format:
        ``(protocol, cidr_block)`` or ``(protocol, security_group)``.

        * **protocol** (*string*):

            * ``HTTP``
            * ``HTTPS``
            * ``DNS``
            * ``TCP:Port`` (e.g., ``TCP:80``)
            * ``UDP:Port`` (e.g., ``UDP:1337``)

        * **cidr_block** (*string*):

            * A range of IP addresses to which outbound traffic is allowed.

        * **security_group** (:class:`~boto.ec2.securitygroup.SecurityGroup`)

            * A :class:`boto.ec2.securitygroup.SecurityGroup` that is allowed egress from the created security group.

    :rtype: :class:`boto.ec2.securitygroup.SecurityGroup`
    :return: An Amazon EC2-VPC Security Group.
    """

    # Defer import to resolve interdependency between .database and .compute modules.
    from .database import INBOUND_PORT

    # Connect to the Amazon Elastic Compute Cloud (Amazon EC2) service.
    ec2_connection = connect_ec2()

    # Generate Security Group name.
    if not name:
        name = '-'.join(['gp', config['PROJECT_NAME'], config['ENVIRONMENT']])

    # Check for existing Security Group.
    if config['CREATION_MODE'] == mode.PERMANENT:
        try:
            existing_security_group = ec2_connection.get_all_security_groups(filters={'group-name': name,})
            if len(existing_security_group):
                logger.info('Found existing Security Group (%s).' % name)
                return existing_security_group[-1]
        except boto.exception.EC2ResponseError as error:
            if error.code == 'InvalidGroup.NotFound': # The requested Security Group doesn't exist.
                pass

    if database_backend:
        allowed_outbound_traffic.append(('TCP:%d' % INBOUND_PORT[database_backend], '0.0.0.0/0'))

    # Create Security Group.
    logger.info('Creating Security Group (%s).' % name)
    security_group = ec2_connection.create_security_group(name, 'Security Group Description', vpc_id=vpc.id)
    logger.info('Created Security Group (%s).' % name)

    # Set up inbound/outbound rules.
    ec2_connection.revoke_security_group_egress(security_group.id, -1, from_port=0, to_port=65535, cidr_ip='0.0.0.0/0')
    for (protocol, target, rule_type) in [(traffic[0].upper(), traffic[1], 'inbound') for traffic in allowed_inbound_traffic] + \
                                         [(traffic[0].upper(), traffic[1], 'outbound') for traffic in allowed_outbound_traffic]:

        # Determine whether target is a CIDR block or a Security Group.
        is_cidr_ip = re.search('^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.'+      # A in A.B.C.D
                               '(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.' +      # B in A.B.C.D
                               '(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)\.' +      # C in A.B.C.D
                               '(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)'   +      # D in A.B.C.D
                               '(/(([1-2]?[0-9])|(3[0-2])))$', str(target)) # /0 through /32
        target_cidr_ip = target if is_cidr_ip else None
        target_group = ec2_connection.get_all_security_groups(group_ids=[target])[-1] if not is_cidr_ip else None

        # Determine port range for TCP and UDP rules.
        port = None
        if protocol[:3] in ['TCP', 'UDP']:
            protocol, port = itemgetter(0, 1)(protocol.split(':'))
            if re.search(r'^\d+\-\d+$', port):
                from_port, to_port = itemgetter(0, 1)(port.split('-'))
            else:
                from_port, to_port = port, port

        # Create inbound rules.
        if rule_type == 'inbound':
            if protocol == 'HTTP':
                security_group.authorize(ip_protocol='tcp', from_port=80, to_port=80, src_group=target_group, cidr_ip=target_cidr_ip)
            elif protocol == 'HTTPS':
                security_group.authorize(ip_protocol='tcp', from_port=443, to_port=443, src_group=target_group, cidr_ip=target_cidr_ip)
            elif protocol == 'TCP':
                security_group.authorize(ip_protocol='tcp', from_port=int(from_port), to_port=int(to_port), src_group=target_group, cidr_ip=target_cidr_ip)
            elif protocol == 'UDP':
                security_group.authorize(ip_protocol='udp', from_port=int(from_port), to_port=int(to_port), src_group=target_group, cidr_ip=target_cidr_ip)
            logger.info('Security Group (%s) allowed inbound %s traffic from %s.' % (name, protocol + (' Port %s' % port if port else ''), target))

        # Create outbound rules.
        if rule_type == 'outbound':
            if protocol == 'HTTP':
                ec2_connection.authorize_security_group_egress(security_group.id, 'tcp', from_port=80, to_port=80, src_group_id=target_group, cidr_ip=target_cidr_ip)
            elif protocol == 'HTTPS':
                ec2_connection.authorize_security_group_egress(security_group.id, 'tcp', from_port=443, to_port=443, src_group_id=target_group, cidr_ip=target_cidr_ip)
            elif protocol == 'DNS':
                ec2_connection.authorize_security_group_egress(security_group.id, 'tcp', from_port=53, to_port=53, src_group_id=target_group, cidr_ip=target_cidr_ip)
                ec2_connection.authorize_security_group_egress(security_group.id, 'udp', from_port=53, to_port=53, src_group_id=target_group, cidr_ip=target_cidr_ip)
            elif protocol == 'TCP':
                 ec2_connection.authorize_security_group_egress(security_group.id, 'tcp', from_port=int(from_port), to_port=int(to_port), src_group_id=target_group, cidr_ip=target_cidr_ip)
            elif protocol == 'UDP':
                 ec2_connection.authorize_security_group_egress(security_group.id, 'udp', from_port=int(from_port), to_port=int(to_port), src_group_id=target_group, cidr_ip=target_cidr_ip)
            logger.info('Security Group (%s) allowed outbound %s traffic to %s.' % (name, protocol + (' Port %s' % port if port else ''), target))

    # Tag Security Group.
    tagged = False
    while not tagged:
        try:
            tagged = ec2_connection.create_tags(security_group.id, {'Name': name,
                                                                    'Project': config['PROJECT_NAME'],
                                                                    'Environment': config['ENVIRONMENT'],})
        except boto.exception.EC2ResponseError as error:
            if error.code == 'InvalidID': # Security Group hasn't registered with EC2 service yet.
                pass
            else:
                raise boto.exception.EC2ResponseError

    return security_group


def create_load_balancer(subnets, name=None, security_groups=None, ssl_certificate=None):
    """
    Create an Elastic Load Balancer (ELB).

    :type subnets: list
    :param subnets: A list of :class:`boto.vpc.subnet.Subnet` objects that
        will share inbound traffic.

    :type name: string
    :param name: An *optional* name for the Load Balancer. A name will be
        generated from the current project name, if one is not specified.

    :type security_groups: list
    :param security_groups: An *optional* list of
        :class:`boto.ec2.securitygroup.SecurityGroup` objects that the Load
        Balancer will join.

        * See also: :func:`sky.compute.create_security_group`.

    :type ssl_certificate: string
    :param ssl_certificate: The Amazon Resource Name (ARN) of an uploaded SSL
        Certificate. This is required only if HTTPS/SSL load balancer listeners
        are desired.

        * See also: :func:`sky.security.upload_ssl_certificate`.

    :rtype: :class:`boto.ec2.elb.loadbalancer.LoadBalancer`
    :return: An Elastic Load Balancer (ELB).
    """
    # Connect to the Amazon EC2 Load Balancing (Amazon ELB) service.
    logger.debug('Connecting to the Amazon EC2 Load Balancing (Amazon ELB) service.')
    elb_connection = boto.connect_elb()
    logger.debug('Connected to the Amazon EC2 Load Balancing (Amazon ELB) service.')

    # Generate Elastic Load Balancer (ELB) name.
    if not name:
        name = '-'.join(['elb', config['PROJECT_NAME'], config['ENVIRONMENT']])

    # Check for existing Load Balancer.
    if config['CREATION_MODE'] == mode.PERMANENT:
        try:
            existing_load_balancer = elb_connection.get_all_load_balancers(load_balancer_names=[name])
            if len(existing_load_balancer):
                existing_load_balancer = existing_load_balancer[-1]
                logger.info('Found existing Load Balancer (%s) at (%s).' % (existing_load_balancer.name, existing_load_balancer.dns_name))
                return existing_load_balancer
        except boto.exception.BotoServerError as error:
            if error.code == 'LoadBalancerNotFound': # The requested Load Balancer doesn't exist.
                pass

    # Set up default security group, if necessary
    if not security_groups:
        # Connect to the Amazon Virtual Private Cloud (Amazon VPC) service.
        vpc_connection = connect_vpc()

        # Get VPC from Subnets.
        vpc_id = subnets[-1].vpc_id
        vpc = vpc_connection.get_all_vpcs(vpc_ids=[vpc_id])[-1]

        security_group_name = '-'.join(['gp', config['PROJECT_NAME'], config['ENVIRONMENT'], 'elb'])
        security_groups = [create_security_group(vpc,
                                                 name=security_group_name,
                                                 allowed_inbound_traffic=[('HTTP',   '0.0.0.0/0')
                                                                         ,('HTTPS',  '0.0.0.0/0')],
                                                 allowed_outbound_traffic=[('HTTP',  '0.0.0.0/0')
                                                                          ,('HTTPS', '0.0.0.0/0')
                                                                          ,('DNS',   '0.0.0.0/0')])]

    # Delete existing Elastic Load Balancer (ELB).
    logger.info('Deleting Elastic Load Balancer (%s).' % name)
    try:
        elb_connection.delete_load_balancer(name)
    except boto.exception.BotoServerError as error:
        if error.status == 400: # Bad Request
            logger.error('Couldn\'t delete Elastic Load Balancer (%s) due to a malformed request %s: %s.' % (name, error.status, error.reason))
        if error.status == 404: # Not Found
            logger.error('Elastic Load Balancer (%s) was not found. Error %s: %s.' % (name, error.status, error.reason))
    logger.info('Deleted Elastic Load Balancer (%s).' % name)

    # Set up basic HTTP listener.
    complex_listeners = [(80, 80, 'HTTP', 'HTTP')]

    # Add HTTPS listener if a SSL certificate was specified.
    if ssl_certificate:
        complex_listeners.append((443, 443, 'HTTPS', 'HTTPS', ssl_certificate))

    # Create Elastic Load Balancer (ELB).
    logger.info('Creating Elastic Load Balancer (%s).' % name)
    load_balancer = elb_connection.create_load_balancer(name, # name
                                                        None, # zones             # Valid only for load balancers in EC2-Classic.
                                                        listeners=None,
                                                        subnets=[subnet.id for subnet in subnets],
                                                        security_groups=[security_group.id for security_group in security_groups],
                                                        scheme='internet-facing', # Valid only for load balancers in EC2-VPC.
                                                        complex_listeners=complex_listeners)
    logger.info('Created Elastic Load Balancer (%s).' % name)

    return load_balancer

def create_nat_instances(vpc, public_subnets, private_subnets, security_groups=None, image_id=None):

    if not len(public_subnets) == len(private_subnets):
        raise RuntimeError('The number of Public/Private Subnets must match (Public: %d Private: %d).' % (len(public_subnets), len(private_subnets)))

    subnet_pairs = list(zip(sorted(public_subnets, key=lambda x: x.availability_zone), sorted(private_subnets, key=lambda x: x.availability_zone)))

    # Create NAT instances.
    nat_instances = list()
    for (public_subnet, private_subnet) in subnet_pairs:
        nat_instance = create_nat_instance(vpc, public_subnet, private_subnet, name=None, security_groups=None, image_id=None)
        nat_instances.append(nat_instance)

    return nat_instances

def create_nat_instance(vpc, public_subnet, private_subnet, name=None, security_groups=None, image_id=None):
    # Connect to the Amazon Elastic Compute Cloud (Amazon EC2) service.
    ec2_connection = connect_ec2()

    # Connect to the Amazon Virtual Private Cloud (Amazon VPC) service.
    vpc_connection = connect_vpc()

    # Generate name, if one was not specified.
    if not name:
        name = '-'.join(['ec2', config['PROJECT_NAME'], config['ENVIRONMENT'], public_subnet.availability_zone, 'nat'])

    # Check for existing NAT Server.
    if config['CREATION_MODE'] == mode.PERMANENT:
         nat_instances = get_instances(name=name, role='nat')
         if len(nat_instances):
             logger.info('Found existing NAT Server (%s).' % name)
             return nat_instances

    # Create Security Group, if one was not specified.
    if not security_groups:
        sg_name = '-'.join(['gp', config['PROJECT_NAME'], config['ENVIRONMENT'], public_subnet.availability_zone, 'nat'])
        security_groups = [create_security_group(vpc, name=sg_name
                                                    , allowed_inbound_traffic=[('HTTP',   private_subnet.cidr_block)
                                                                              ,('HTTPS',  private_subnet.cidr_block)]
                                                    , allowed_outbound_traffic=[('HTTP',  '0.0.0.0/0')
                                                                               ,('HTTPS', '0.0.0.0/0')
                                                                               ,('DNS',   '0.0.0.0/0')])]

    # Get Amazon Linux VPC NAT AMI, if one was not specified.
    if not image_id:
        image_id = get_nat_image()

    # Create NAT Instance.
    nat_instance = create_instance(public_subnet, name=name, role='nat', security_groups=security_groups, image_id=image_id.id, internet_addressable=True)[0]

    # Disable source/destination checking.
    ec2_connection.modify_instance_attribute(nat_instance.id, attribute='sourceDestCheck', value=False, dry_run=False)

    # Create Route table.
    route_table_name = '-'.join(['rtb', config['PROJECT_NAME'], config['ENVIRONMENT'], public_subnet.availability_zone, 'private'])
    route_table = create_route_table(vpc, name=route_table_name, internet_access=False)

    # Wait for NAT instance to run.
    while (nat_instance.state == 'pending'):
        logger.debug('Waiting for NAT instance to run (%s)...' % nat_instance.id)
        time.sleep(1)
        nat_instance.update()

    # Add route to NAT Instance to Route Table.
    vpc_connection.create_route(route_table.id,    # route_table_id
                                '0.0.0.0/0',       # destination_cidr_block
                                gateway_id=None,
                                instance_id=nat_instance.id,
                                interface_id=None,
                                vpc_peering_connection_id=None,
                                dry_run=False)

    # Check for existing Route Table association.
    route_tables = vpc_connection.get_all_route_tables(filters={'vpc-id': vpc.id,})
    existing_association = [association.id for route_table in route_tables for association in route_table.associations if association.subnet_id == private_subnet.id]
    existing_association = existing_association[0] if existing_association else None

    # Associate Private Subnet to Route Table.
    if existing_association:
        association = vpc_connection.replace_route_table_association_with_assoc(existing_association, # association_id
                                                                                route_table.id,       # route_table_id
                                                                                dry_run=False)
    else:
        association = vpc_connection.associate_route_table(route_table.id,    # route_table_id
                                                           private_subnet.id, # subnet_id
                                                           dry_run=False)
    if len(association):
        logger.debug('Subnet (%s) associated to (%s).' % (private_subnet.id, route_table.name))
    else:
        logger.error('Subnet (%s) not associated to (%s).' % (private_subnet.id, route_table.name))

    # Clean up unused/orphaned Route Tables.
    route_tables = vpc_connection.get_all_route_tables(filters={'vpc-id': vpc.id,})
    main_route_table = vpc_connection.get_all_route_tables(filters={'vpc-id': vpc.id,
                                                                    'association.main': 'true'})[0] # Affected by boto Issue #1742 : https://github.com/boto/boto/issues/1742
    empty_route_tables = [route_table for route_table in route_tables if not len(route_table.associations) and not route_table.id == main_route_table.id]
    for route_table in empty_route_tables:
        try:
            vpc_connection.delete_route_table(route_table.id, dry_run=False)
        except boto.exception.EC2ResponseError as error:
            if error.code == 'DependencyViolation': # Route Table was not actually empty.
                pass

    return nat_instance

def create_instances(vpc, subnets, role=None, security_groups=None, script=None, instance_profile=None, os='ubuntu', image_id=None, key_name=None, internet_addressable=False):
    # Create a security group, if a security group was not specified.
    if not security_groups:
        security_groups = [create_security_group(vpc, allowed_inbound_traffic=[('HTTP',   '0.0.0.0/0')
                                                                              ,('HTTPS',  '0.0.0.0/0')]
                                                    , allowed_outbound_traffic=[('HTTP',  '0.0.0.0/0')
                                                                               ,('HTTPS', '0.0.0.0/0')
                                                                               ,('DNS',   '0.0.0.0/0')])]

    # Create EC2 instances.
    instances = list()
    for subnet in subnets:
        instance = create_instance(subnet, role=role, security_groups=security_groups, script=script, instance_profile=instance_profile, os=os, image_id=image_id, key_name=key_name, internet_addressable=internet_addressable)
        instances = instances + instance
    return instances

def create_instance(subnet, name=None, role=None, security_groups=None, script=None, instance_profile=None, os='ubuntu', image_id=None, key_name=None, internet_addressable=False):
    # Set up dictionary of OSes and their associated quick-start Amazon Machine Images (AMIs).
    ami = {
        'amazon-linux': 'ami-146e2a7c',
        'redhat':       'ami-12663b7a',
        'suse':         'ami-aeb532c6',
        'ubuntu':       'ami-9a562df2',
    }

    # Connect to the Amazon Elastic Compute Cloud (Amazon EC2) service.
    ec2_connection = connect_ec2()

    # Determine whether to use a start-up AMI or a specific AMI.
    if image_id:
        image = ec2_connection.get_image(image_id)
        if not image:
            raise RuntimeError('The specified Amazon Machine Image (AMI) could not be found (%s).' % image_id)
    else:
        image_id = ami[os]

    # Generate EC2 Instance name, if one was not specified.
    if not name:
        random_id = '{:08x}'.format(random.randrange(2**32))
        name = '-'.join(['ec2', config['PROJECT_NAME'], config['ENVIRONMENT'], random_id])

    # Generate Elastic Network Interface (ENI) name.
    eni_name = '-'.join(['eni', name.replace('ec2-', '')])

    # Create Elastic Network Interface (ENI) specification.
    interface = boto.ec2.networkinterface.NetworkInterfaceSpecification(subnet_id=subnet.id,
                                                                        groups=[security_group.id for security_group in security_groups],
                                                                        associate_public_ip_address=internet_addressable)
    interfaces = boto.ec2.networkinterface.NetworkInterfaceCollection(interface)

    # Create EC2 Reservation.
    logger.info('Creating EC2 Instance (%s) in %s.' % (name, subnet.availability_zone))
    reservation = ec2_connection.run_instances(image_id,                 # image_id
                                               key_name=key_name,
                                               instance_type='t2.micro',
                                               instance_profile_name=instance_profile.name if instance_profile else None,
                                               network_interfaces=interfaces,
                                               user_data=script)
    logger.info('Created EC2 Instance (%s).' % name)

    # Get EC2 Instances.
    instances = [instances for instances in reservation.instances]

    # Tag EC2 Instances.
    tagged = False
    while not tagged:
        try:
            tagged = ec2_connection.create_tags([instance.id for instance in instances], {'Name': name,
                                                                                          'Project': config['PROJECT_NAME'],
                                                                                          'Environment': config['ENVIRONMENT'],
                                                                                          'Role': role if role else '',})
        except boto.exception.EC2ResponseError as error:
            if error.code == 'InvalidInstanceID.NotFound': # Instance hasn't registered with EC2 service yet.
                pass
            else:
                raise boto.exception.EC2ResponseError

    # Get Elastic Network Interface (ENI) attached to instances.
    interfaces = None
    while not interfaces:
        try:
            interfaces = ec2_connection.get_all_network_interfaces(filters={'attachment.instance-id': [instance.id for instance in instances]})
        except boto.exception.EC2ResponseError as error:
            if error.code == 'InvalidInstanceID.NotFound': # Instance hasn't registered with EC2 service yet.
                pass
            else:
                raise boto.exception.EC2ResponseError

    # Tag Elastic Network Interface (ENI).
    tagged = False
    while not tagged:
        try:
            tagged = ec2_connection.create_tags([interface.id for interface in interfaces], {'Name': eni_name,
                                                                                             'Project': config['PROJECT_NAME'],
                                                                                             'Environment': config['ENVIRONMENT']})
        except boto.exception.EC2ResponseError as error:
            if error.code == 'InvalidNetworkInterfaceID.NotFound': # ENI hasn't registered with EC2 service yet.
                pass
            else:
                raise boto.exception.EC2ResponseError

    # Refresh EC2 instance objects.
    reservations = ec2_connection.get_all_instances(instance_ids=[instance.id for instance in instances])
    instances = [instance for reservation in reservations for instance in reservation.instances]

    return instances

def get_nat_image(paravirtual=False):
    # Connect to the Amazon Elastic Compute Cloud (Amazon EC2) service.
    ec2_connection = connect_ec2()

    # Get paravirtual (PV) or hardware virtual machine (HVM) Amazon Linux VPC NAT AMIs.
    images = ec2_connection.get_all_images(filters={'owner-alias': 'amazon',
                                                    'name': 'amzn-ami-vpc-nat-' + ('pv' if paravirtual else 'hvm') + '*',})

    # Return the most recent AMI.
    image = sorted(images, key=lambda x: x.name.split('-')[5])[-1]
    return image

def run(script, command):
    script += '\n' + command
    return script

def install_package(script, package_name):
    script += '\n' + 'apt-get --yes --quiet install %s' % package_name
    return script

def register_instances(load_balancer, instances):
    # Connect to the Amazon EC2 Load Balancing (Amazon ELB) service.
    logger.debug('Connecting to the Amazon EC2 Load Balancing (Amazon ELB) service.')
    elb_connection = boto.connect_elb()
    logger.debug('Connected to the Amazon EC2 Load Balancing (Amazon ELB) service.')

    logger.info('Registering (%s) with Load Balancer (%s).' % (', '.join([instance.tags['Name'] for instance in instances]) if len(instances) > 1 \
                                                               else instances[-1].tags['Name'], \
                                                               load_balancer.name))
    elb_connection.register_instances(load_balancer.name, [instance.id for instance in instances])
    logger.info('Registered (%s) with Load Balancer (%s).' % (', '.join([instance.tags['Name'] for instance in instances]) if len(instances) > 1 \
                                                              else instances[-1].tags['Name'], \
                                                              load_balancer.name))

def deregister_instances(load_balancer, instances):
    # Connect to the Amazon EC2 Load Balancing (Amazon ELB) service.
    logger.debug('Connecting to the Amazon EC2 Load Balancing (Amazon ELB) service.')
    elb_connection = boto.connect_elb()
    logger.debug('Connected to the Amazon EC2 Load Balancing (Amazon ELB) service.')

    logger.info('Deregistering (%s) from Load Balancer (%s).' % (', '.join([instance.tags['Name'] for instance in instances]) if len(instances) > 1 \
                                                                 else instances[-1].tags['Name'], \
                                                                 load_balancer.name))
    elb_connection.deregister_instances(load_balancer.name, [instance.id for instance in instances])
    logger.info('Deregistered (%s) from Load Balancer (%s).' % (', '.join([instance.tags['Name'] for instance in instances]) if len(instances) > 1 \
                                                                else instances[-1].tags['Name'], \
                                                                load_balancer.name))

def get_instances(name=None, role=None, state='running'):
    # Connect to the Amazon Elastic Compute Cloud (Amazon EC2) service.
    ec2_connection = connect_ec2()

    # Set up filters.
    filters = {}
    filters['instance-state-name'] = state
    filters['tag:Project'] = config['PROJECT_NAME']
    filters['tag:Environment'] = config['ENVIRONMENT']

    if name:
        filters['tag:Name'] = name

    if role:
        filters['tag:Role'] = role

    # Get reservations by tag.
    reservations = ec2_connection.get_all_instances(filters=filters)
    
    # Get instances from reservations.
    instances = [instance for reservation in reservations for instance in reservation.instances]
    
    return instances
    
def terminate_instances(instances):
    # Connect to the Amazon Elastic Compute Cloud (Amazon EC2) service.
    ec2_connection = connect_ec2()
    
    # Terminate EC2 instances.
    if instances:
        logger.info('Terminating (%s).' % (', '.join([instance.tags['Name'] for instance in instances]) if len(instances) > 1 \
                                                     else instances[-1].tags['Name']))
        ec2_connection.terminate_instances(instance_ids=[instance.id for instance in instances])
        logger.info('Terminated (%s).' % (', '.join([instance.tags['Name'] for instance in instances]) if len(instances) > 1 \
                                                    else instances[-1].tags['Name']))

def rotate_instances(load_balancer, instances, terminate_outgoing_instances=True):
    # Connect to the Amazon Elastic Compute Cloud (Amazon EC2) service.
    ec2_connection = connect_ec2()

    # Retrieve outgoing EC2 instances.
    old_reservations = ec2_connection.get_all_instances(instance_ids=[old_instance.id for old_instance in load_balancer.instances]) if load_balancer.instances else None
    old_instances = [instance for reservation in old_reservations for instance in reservation.instances] if load_balancer.instances else None

    # Register incoming EC2 instances with the Load Balancer.
    register_instances(load_balancer, instances)

    # Rotate EC2 instances.
    if old_instances:
        new_instance_names = ', '.join([instance.tags['Name'] for instance in instances]) if len(instances) > 1 \
                             else instances[-1].tags['Name']
        old_instance_names = ', '.join([instance.tags['Name'] for instance in old_instances]) if len(old_instances) > 1 \
                             else old_instances[-1].tags['Name']
        logger.info('Rotating incoming EC2 Instances (%s) and outgoing EC2 instances (%s) under Load Balancer (%s).' % (new_instance_names,
                                                                                                                        old_instance_names,
                                                                                                                        load_balancer.name))
        # Determine incoming EC2 instance states with respect to the Load Balancer.
        instance_states = load_balancer.get_instance_health(instances=[instance.id for instance in instances])

        # Rotate EC2 instances with a new EC2. instance has come into service.
        while 'OutOfService' in [instance_state.state for instance_state in instance_states]:
            # Refresh incoming EC2 instance states with respect to the Load Balancer.
            instance_states = load_balancer.get_instance_health(instances=[instance.id for instance in instances])

            # Terminate outgoing EC2 instance when an incoming EC2 instance has come into service.
            for instance_id in [instance_state.instance_id for instance_state in instance_states if instance_state.state == 'InService']:
                # Get incoming instance.
                instance = next(instance for instance in instances if instance.id == instance_id)
                logger.info('EC2 Instance (%s) has come into service.' % instance.tags['Name'])

                # Get outgoing EC2 instance.
                old_instance = next(old_instance for old_instance in old_instances if old_instance.subnet_id == instance.subnet_id)

                # Deregister outgoing EC2 instance from Load Balancer.
                deregister_instances(load_balancer, [old_instance])

                if terminate_outgoing_instances:
                    # Terminate outgoing EC2 instance.
                    terminate_instances([old_instance])

                # Remove incoming EC2 instance from list.
                instances.remove(instance)

            # Throttle EC2 instance rotation.
            time.sleep(5)

        logger.info('Rotated incoming EC2 Instances (%s) and outgoing EC2 instances (%s) under Load Balancer (%s).' % (new_instance_names,
                                                                                                                       old_instance_names,
                                                                                                                       load_balancer.name))

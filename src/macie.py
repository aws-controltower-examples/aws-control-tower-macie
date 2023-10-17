import boto3
import os
import cfnresponse
from botocore.config import Config
from botocore.exceptions import ClientError

macie_master_account=os.environ['MACIE_MASTER_ACCOUNT']
role_to_assume=os.environ['ROLE_TO_ASSUME']

org_client=boto3.client('organizations')

config=Config(
    retries={
        'max_attempts':10,
        'mode':'standard'
    }
)

def lambda_handler(event, context):
    macie_regions=boto3.Session().get_available_regions('macie2')
    control_tower_regions=get_control_tower_regions()
    macie_master_account_session=assume_role(macie_master_account, role_to_assume)
    accounts=get_all_accounts()
    if 'RequestType' in event:
        if (event['RequestType'] == 'Create' or event['RequestType'] == 'Update'):
            try: 
                org_client.enable_aws_service_access(
                    ServicePrincipal='macie.amazonaws.com'
                )
                for region in control_tower_regions:
                    if region in macie_regions:
                        enable_macie_master(macie_master_account_session, region)
                        enable_macie_member(macie_master_account_session, accounts, region)
                cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
            except ClientError as error:
                print(error)
                cfnresponse.send(event, context, cfnresponse.FAILED, error)  
        elif event['RequestType'] == 'Delete':
            try:
                for region in control_tower_regions:
                    if region in macie_regions:
                        macie_client=boto3.client('macie2', region_name=region)
                        try:
                            macie_client.disable_organization_admin_account(
                                adminAccountId=macie_master_account
                            )
                        except ClientError as error:
                            print(f"Delegated Administration for Amazon Macie has been disabled in {region}.")
                        for account in accounts:
                            member_session=assume_role(account['Id'], role_to_assume)
                            member_client=member_session.client('macie2', region_name=region)
                            macie_admin_client=macie_master_account_session.client('macie2', region_name=region)
                            try:
                                macie_admin_client.delete_member(
                                    id=account['Id']
                                )
                            except ClientError as error:
                                print(f"Unable to delete {account['Id']} in {region} as a member from Amazon Macie as it's not enabled.")
                            try:
                                member_client.disable_macie()
                                print(f"Amazon Macie has been disabled in {region}.")
                            except ClientError as error:
                                print(f"Unable to disable Amazon Macie in {account['Id']} in {region} as it's not enabled.")
                cfnresponse.send(event, context, cfnresponse.SUCCESS, {})
            except ClientError as error:
                print(error)
                cfnresponse.send(event, context, cfnresponse.FAILED, error)  
        else:
            try: 
                org_client.enable_aws_service_access(
                    ServicePrincipal='macie.amazonaws.com'
                )
                for region in control_tower_regions:
                    if region in macie_regions:
                        enable_macie_master(macie_master_account_session, region)
                        enable_macie_member(macie_master_account_session, accounts, region)
            except ClientError as error:
                print(f"AWS Service Access has already been configured for Amazon Macie.")
        

def assume_role(aws_account_id, role_to_assume):
    sts_client=boto3.client('sts')
    response=sts_client.assume_role(
        RoleArn=f'arn:aws:iam::{aws_account_id}:role/{role_to_assume}',
        RoleSessionName='EnableSecurityHub'
    )
    sts_session=boto3.Session(
        aws_access_key_id=response['Credentials']['AccessKeyId'],
        aws_secret_access_key=response['Credentials']['SecretAccessKey'],
        aws_session_token=response['Credentials']['SessionToken']
    )
    print(f"Assumed session for Account ID: {aws_account_id}.")
    return sts_session

def get_control_tower_regions():
    cloudformation_client=boto3.client('cloudformation')
    control_tower_regions=set()
    try:
        stack_instances=cloudformation_client.list_stack_instances(
            StackSetName="AWSControlTowerBP-BASELINE-CONFIG"
        )
        for stack in stack_instances['Summaries']:
            control_tower_regions.add(stack['Region'])
    except ClientError as error:
        print(error)
    print(f"Control Tower Regions: {list(control_tower_regions)}")
    return list(control_tower_regions)

def get_all_accounts():
    all_accounts=[]
    active_accounts=[]
    token_tracker={}
    while True:
        member_accounts=org_client.list_accounts(
            **token_tracker
        )
        all_accounts.extend(member_accounts['Accounts'])
        if 'NextToken' in member_accounts:
            token_tracker['NextToken'] = member_accounts['NextToken']
        else:
            break
    for account in all_accounts:
        if account['Status'] == 'ACTIVE':
            active_accounts.append(account)
    return active_accounts

def enable_macie_master(macie_master_account_session, region):
    macie_client=boto3.client('macie2', region_name=region)
    macie_admin_client=macie_master_account_session.client('macie2', region_name=region)
    delegated_admin=macie_client.list_organization_admin_accounts()['adminAccounts']
    if len(delegated_admin) > 0:
        print(f"Delegated Administration for Amazon Macie has already been enabled in {region}.")
    else:
        try:
            macie_client.enable_organization_admin_account(
                adminAccountId=macie_master_account
            )
            print(f"Delegated Administration for Amazon Macie has been enabled in {region}.")
        except ClientError as error:
            print(f"Delegated Administration for Amazon Macie has already been configured in {region}.")
    try:
        macie_admin_client.update_organization_configuration(
            autoEnable=True
        )
    except ClientError as error:
        print(f"Unable to update the Organization Configuration for Amazon Macie in {region}.")

def enable_macie_member(macie_master_account_session, accounts, region):
    macie_admin_client=macie_master_account_session.client('macie2', region_name=region, config=config)
    details=[]
    for account in accounts:
        if account['Id'] != macie_master_account:
            member_session=assume_role(account['Id'], role_to_assume)
            member_client=member_session.client('macie2', region_name=region)
            details.append(
                {
                    'accountId': account['Id'],
                    'email': account['Email']
                }
            )
            try:
                member_client.enable_macie(
                    findingPublishingFrequency='FIFTEEN_MINUTES',
                    status='ENABLED'
                )
                print(f"Amazon Macie has been enabled in Account ID: {account['Id']} in {region}.")
            except ClientError as error:
                print(f"Amazon Macie has already been enabled in Account ID: {account['Id']} in {region}.")
    details_batch=chunks(details, 1)
    try:
        for b in details_batch:
            response=macie_admin_client.create_member(
                account=b[0]
            )
    except ClientError as error:
        print(f"Unable to add Account ID: {b[0]['accountId']} in {region} as a Member to for Amazon Macie. Error: {error}.")

def chunks(l, n):
    for i in range(0, len(l), n):
        yield l[i:i+n]
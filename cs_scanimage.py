from __future__ import print_function
import argparse
import json
import logging
import sys
from os import environ as env
from enum import Enum
import subprocess  # nosec
import time
import getpass
import requests

try:
    import docker
except ModuleNotFoundError:
    import podman as docker

registry_url_map = {
    'us-1': 'container-upload.us-1.crowdstrike.com',
    'us-2': 'container-upload.us-2.crowdstrike.com',
    'eu-1': 'container-upload.eu-1.crowdstrike.com',
    'us-gov-1': 'container-upload.laggar.gcw.crowdstrike.com',
}
auth_url_map = {
    'us-1': 'https://api.crowdstrike.com',
    'us-2': 'https://api.us-2.crowdstrike.com',
    'eu-1': 'https://api.eu-1.crowdstrike.com',
    'us-gov-1': 'https://api.laggar.gcw.crowdstrike.com',
}


logging.basicConfig(stream=sys.stdout, format='%(levelname)-8s%(message)s')
log = logging.getLogger('cs_scanimage')


# class to simulate scanning
class ScanImage(Exception):
    """Scanning Image Tasks"""

    def __init__(self, client_id, client_secret, repo, tag, client, cloud):
        self.client_id = client_id
        self.client_secret = client_secret
        self.repo = repo
        self.tag = tag
        self.client = client
        self.server_domain = registry_url_map[cloud]
        self.auth_url = "%s/oauth2/token" % (auth_url_map[cloud])

    # Step 1: perform container tag to the registry corresponding to the cloud entered
    def container_tag(self):
        local_tag = "%s:%s" % (self.repo, self.tag)
        url_tag = "%s/%s" % (self.server_domain, self.repo)

        container_image = ''.join((''.join(img.attrs["RepoTags"])
                                   for img in self.client.images.list(filters={"reference": local_tag})))

        if not container_image:
            log.info("Pulling container image: '%s'", local_tag)
            self.client.images.pull(local_tag)

        log.info("Tagging '%s' to '%s:%s'", local_tag, url_tag, self.tag)
        self.client.images.get(local_tag).tag(url_tag, self.tag, force=True)

    # Step 2: login using the credentials supplied
    def container_login(self):
        log.info("Performing login to CrowdStrike Image Assessment Service")
        login = self.client.login(username=self.client_id,
                                  password=self.client_secret, registry=self.server_domain, reauth=True)
        try:
            log.info(login["Status"])
        except TypeError:
            command = ["/usr/bin/podman", "login"]
            command.extend(['--username', self.client_id])
            command.extend(['--password', self.client_secret])
            command.append(self.server_domain)
            result = subprocess.run(command, shell=False, encoding="utf-8",  # nosec
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            if result.returncode != 0:
                raise Exception(result.stderr.strip()) from None

            log.info(result.stdout.strip())

    # Step 3: perform container push using the repo and tag supplied
    def container_push(self):
        image_str = "%s/%s:%s" % (self.server_domain, self.repo, self.tag)
        log.info("Performing container push to %s", image_str)

        try:
            image_push = self.client.images.push(
                image_str, stream=True, decode=True)
        except AttributeError:
            image_push = self.client.push(image_str, stream=True, decode=True)

        for line in image_push:
            if 'error' in line:
                raise APIError('docker_push ' + line['error'])

            if 'status' in line and line['status'] == 'Pushing':
                print("Pushing {}".format([line.get(key) for key in ['progress', 'progressDetails']]), end='\r')
            elif 'status' in line:
                log.info("Docker: %s", line['status'])
            else:
                log.debug(line)

    # Step 4: get the api token used for getting the scan report
    def get_api_token(self):
        log.info("Authenticating with CrowdStrike Falcon API")
        post_url = self.auth_url
        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret
        }
        headers = {
            "Content-Type": "application/x-www-form-urlencoded"
        }
        resp = requests.post(post_url, data=payload, headers=headers)
        if resp.status_code in (200, 201):
            return resp.json()["access_token"]

        raise APIError('POST ' + post_url + ' {}'.format(resp.status_code))

    # Step 5: poll and get scanreport for specified amount of retries
    def get_scanreport(self, token, retry_count):
        log.info("Downloading Image Scan Report")
        scanreport_endpoint = "/reports?"
        server_url = "https://%s" % (self.server_domain)
        scanreport_url = "%s%s" % (server_url, scanreport_endpoint)
        sleep_seconds = 10
        get_url = "%srepository=%s&tag=%s" % (
            scanreport_url, self.repo, self.tag)

        for count in range(retry_count):
            time.sleep(sleep_seconds)
            log.debug("retry count %s", count)
            resp = requests.get(get_url, auth=BearerAuth(token))
            if resp.status_code != 200:
                log.info(
                    "Scan report is not ready yet, retrying in %s seconds", sleep_seconds)
            else:
                return ScanReport(resp.json())
        log.error("Retries exhausted")
        raise APIError('GET ' + get_url + ' {}'.format(resp.status_code))


class ScanReport(dict):
    """Summary Report of the Image Scan"""
    vuln_str_key_1 = 'Vulnerabilities'
    details_str_key = 'Details'
    detect_str_key = 'Detections'

    severity_high = "high"
    type_malware = "malware"
    type_secret = "secret"  # nosec
    type_misconfig = 'misconfiguration'
    type_cis = 'cis'

    def status_code(self):
        vuln_code = self.get_alerts_vuln()
        mal_code = self.get_alerts_malware()
        sec_code = self.get_alerts_secrets()
        mcfg_code = self.get_alerts_misconfig()
        return vuln_code | mal_code | sec_code | mcfg_code

    def export(self, filename):
        with open(filename, 'w', encoding="utf-8") as f:
            f.write(json.dumps(self, indent=4))

    # Step 6: pass the vulnerabilities from scan report,
    # loop through and find high severity vulns
    # return HighVulnerability enum value
    def get_alerts_vuln(self):
        log.info("Searching for vulnerabilities in scan report...")
        critical_score = 2000
        high_score = 500
        medium_score = 100
        low_score = 20
        vuln_score = 0
        vulnerabilities = self[self.vuln_str_key_1]
        if vulnerabilities is not None:
            for vulnerability in vulnerabilities:
                vuln = vulnerability['Vulnerability']
                cve = vuln.get('CVEID', 'CVE-unknown')
                details = vuln.get('Details', {})

                # verify details is a dict
                if isinstance(details, dict):
                    cvss_v3 = details.get('cvss_v3_score', {})
                    severity = cvss_v3.get('severity')
                    if severity is None:
                        cvss_v2 = details.get('cvss_v2_score', {})
                        severity = cvss_v2.get('severity')
                    if severity is None:
                        severity = details.get('severity', '')
                else:
                    severity = ''

                product = vuln.get('Product', {})
                affects = product.get('PackageSource', product)
                log.warning(
                    "%-8s %-16s Vulnerability detected affecting %s", severity, cve, affects)
                if severity.lower() == 'low':
                    vuln_score = vuln_score + low_score
                if severity.lower() == 'medium':
                    vuln_score = vuln_score + medium_score
                if severity.lower() == 'high':
                    vuln_score = vuln_score + high_score
                if severity.lower() == 'critical':
                    vuln_score = vuln_score + critical_score
        return vuln_score

    # Step 7: pass the detections from scan report,
    # loop through and find if detection type is malware
    # return Malware enum value
    def get_alerts_malware(self):
        log.info("Searching for malware in scan report...")
        det_code = 0
        detections = self[self.detect_str_key]
        if detections is not None:
            for detection in detections:
                try:
                    if detection['Detection']['Type'].lower() == self.type_malware:
                        log.warning("Alert: Malware found")
                        det_code = ScanStatusCode.Malware.value
                        break
                except KeyError:
                    continue
        return det_code

    # Step 8: pass the detections from scan report,
    # loop through and find if detection type is secret
    # return Success enum value
    def get_alerts_secrets(self):
        log.info("Searching for leaked secrets in scan report...")
        det_code = 0
        detections = self[self.detect_str_key]
        if detections is not None:
            for detection in detections:
                try:
                    if detection['Detection']['Type'].lower() == self.type_secret:
                        log.error("Alert: Leaked secrets detected")
                        det_code = ScanStatusCode.Secrets.value
                        break
                except KeyError:
                    continue
        return det_code

    # Step 9: pass the detections from scan report,
    # loop through and find if detection type is misconfig
    # return Success enum value
    def get_alerts_misconfig(self):
        log.info("Searching for misconfigurations in scan report...")
        det_code = 0
        detections = self[self.detect_str_key]
        if detections is not None:
            for detection in detections:
                try:
                    if detection['Detection']['Type'].lower() in [self.type_misconfig, self.type_cis]:
                        log.warning("Alert: Misconfiguration found")
                        det_code = ScanStatusCode.Success.value
                        break
                except KeyError:
                    continue
        return det_code


# these statues are returned and bitwise or'ed
class ScanStatusCode(Enum):
    Vulnerability = 1
    Malware = 2
    Secrets = 3
    Success = 0
    ScriptFailure = 10

# api err generated by setting statuses


class APIError(Exception):
    """An API Error Exception"""

    def __init__(self, status):
        self.status = status

    def __str__(self):
        return "APIError: status={}".format(self.status)


class BearerAuth(requests.auth.AuthBase):
    def __init__(self, token):
        self.token = token

    def __call__(self, r):
        r.headers["authorization"] = "Bearer " + self.token
        return r


# The following class was authored by Russell Heilling
# See https://stackoverflow.com/questions/10551117/setting-options-from-environment-variables-when-using-argparse/10551190#10551190
class EnvDefault(argparse.Action):
    def __init__(self, envvar, required=True, default=None, **kwargs):
        if envvar in env:
            default = env[envvar]
        if required and default:
            required = False
        super(EnvDefault, self).__init__(default=default, required=required,
                                         **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)
# End code authored by Russell Heilling


def parse_args():
    parser = argparse.ArgumentParser(
        description='Crowdstrike - scan your container image.')
    required = parser.add_argument_group('required arguments')
    required.add_argument('-u', '--clientid', action=EnvDefault,
                          dest="client_id", envvar='FALCON_CLIENT_ID',
                          help="Falcon OAuth2 API ClientID")
    required.add_argument('-r', '--repo', action=EnvDefault, dest="repo",
                          envvar='CONTAINER_REPO',
                          help="Container image repository")
    required.add_argument('-t', '--tag', action=EnvDefault, dest="tag",
                          default='latest',
                          envvar='CONTAINER_TAG',
                          help="Container image tag")
    required.add_argument('-c', '--cloud-region', action=EnvDefault, dest="cloud",
                          envvar="FALCON_CLOUD_REGION",
                          default='us-1',
                          choices=['us-1', 'us-2', 'eu-1'],
                          help="CrowdStrike cloud region")
    required.add_argument('-s', '--score_threshold', action=EnvDefault, dest="score",
                          default='500',
                          envvar='SCORE',
                          help="Vulnerability score threshold")
    parser.add_argument('--json-report', action=EnvDefault, dest="report",
                        envvar="JSON_REPORT",
                        default=None,
                        required=False,
                        help='Export JSON report to specified file')
    parser.add_argument('--log-level', action=EnvDefault, dest='log_level',
                        envvar="LOG_LEVEL",
                        default='INFO',
                        required=False,
                        choices=['DEBUG', 'INFO',
                                 'WARNING', 'ERROR', 'CRITICAL'],
                        help="Set the logging level")
    required.add_argument('-R', '--retry_count', action=EnvDefault, dest="retry_count",
                          default='10',
                          envvar='RETRY_COUNT',
                          type=int,
                          help="Scan report retry count")
    args = parser.parse_args()
    logging.getLogger().setLevel(args.log_level)

    return args.client_id, args.repo, args.tag, args.cloud, args.score, args.report, args.retry_count


def main():
    try:
        client_id, repo, tag, cloud, score, json_report, retry_count = parse_args()
        client = docker.from_env()
        client_secret = env.get('FALCON_CLIENT_SECRET')
        if client_secret is None:
            print("Please enter your Falcon OAuth2 API Secret")
            client_secret = getpass.getpass()
        scan_image = ScanImage(client_id, client_secret,
                               repo, tag, client, cloud)
        scan_image.container_tag()
        scan_image.container_login()
        scan_image.container_push()
        token = scan_image.get_api_token()

        scan_report = scan_image.get_scanreport(token, retry_count)
        if json_report:
            scan_report.export(json_report)
        f_vuln_score = int(scan_report.get_alerts_vuln())
        f_secrets = int(scan_report.get_alerts_secrets())
        f_malware = int(scan_report.get_alerts_malware())
        scan_report.get_alerts_misconfig()

        if f_secrets == ScanStatusCode.Secrets.value:
            log.error("Exiting: Secrets found in container image")
            sys.exit(ScanStatusCode.Secrets.value)
        if f_malware == ScanStatusCode.Malware.value:
            log.error("Exiting: Malware found in container image")
            sys.exit(ScanStatusCode.Malware.value)
        if f_vuln_score >= int(score):
            log.error(
                "Exiting: Vulnerability score threshold exceeded: '%s' out of '%s'", f_vuln_score, score)
            sys.exit(ScanStatusCode.Vulnerability.value)
        else:
            log.info(
                "Vulnerability score threshold not met: '%s' out of '%s'", f_vuln_score, score)
            sys.exit(ScanStatusCode.Success.value)

    except APIError:
        log.exception("Unable to scan")
        sys.exit(ScanStatusCode.ScriptFailure.value)
    except Exception:  # pylint: disable=broad-except
        log.exception("Unknown error")
        sys.exit(ScanStatusCode.ScriptFailure.value)


if __name__ == "__main__":
    main()

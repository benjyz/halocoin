import json
import os
import time
import uuid

import docker
import requests
import yaml
from docker.errors import ImageNotFound

from halocoin import api
from halocoin import tools
from halocoin.ntwrk import Response
from halocoin.service import Service, threaded, lockit


class PowerService(Service):
    """
    This is the power service, designed for Coinami.
    Power service is similar to a miner. It solves problems and earns you coins.
    Power contacts with sub-authorities in the network to download problems that you are assigned.
    These problems are related to Bioinformatics, DNA mapping.
    After solving these problems, authority verifies the results and rewards you.
    """

    def __init__(self, engine):
        Service.__init__(self, "power")
        self.engine = engine
        self.blockchain = None
        self.clientdb = None
        self.statedb = None
        self.status = "Loading"
        self.description = ""

    def on_register(self):
        self.clientdb = self.engine.clientdb
        self.blockchain = self.engine.blockchain
        self.statedb = self.engine.statedb
        return True

    def on_close(self):
        self.wallet = None
        print('Power is turned off')

    def get_job_status(self, job_id):
        if self.clientdb.get('local_job_repo_' + job_id) is not None:
            job_directory = os.path.join(self.engine.working_dir, 'jobs', job_id)
            result_file = os.path.join(job_directory, 'output', 'result.zip')
            job_file = os.path.join(job_directory, 'coinami.job.json')
            result_exists = os.path.exists(result_file)
            job_exists = os.path.exists(job_file)
            entry = self.clientdb.get('local_job_repo_' + job_id)
            if entry['status'] == 'executed' or entry['status'] == 'downloaded':
                if result_exists:
                    return "executed"
                elif job_exists:
                    return "downloaded"
                else:
                    return "assigned"
            else:
                return entry['status']
        else:
            return "null"

    @lockit('power')
    def get_status(self):
        return self.status

    @lockit('power')
    def set_status(self, status, description=""):
        self.status = status
        self.description = description
        api.power_status()

    def initiate_job(self, job_id):
        print('Assigned {}'.format(job_id))
        self.clientdb.put('local_job_repo_' + job_id, {
            "status": "assigned",
        })

    def download_job(self, job_id):
        print('Downloading {}'.format(job_id))
        job = self.statedb.get_job(job_id)
        job_directory = os.path.join(self.engine.working_dir, 'jobs', job_id)
        if not os.path.exists(job_directory):
            os.makedirs(job_directory)
        job_file = os.path.join(job_directory, 'job.cfq.gz')

        auth = self.statedb.get_auth(job['auth'])
        endpoint = auth['host'] + "/job_download/{}".format(job_id)
        secret_message = str(uuid.uuid4())
        payload = yaml.dump({
            "message": tools.det_hash(secret_message),
            "signature": tools.sign(tools.det_hash(secret_message), self.wallet.privkey),
            "pubkey": self.wallet.get_pubkey_str()
        })
        r = requests.get(endpoint, stream=True, data={
            'payload': payload
        })
        downloaded = 0
        total_length = int(r.headers.get("Content-Length"))
        with open(job_file, 'wb') as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:  # filter out keep-alive new chunks
                    f.write(chunk)
                    downloaded += 1024*1024
                    self.set_status("Downloading... {}/{}".format(
                        tools.readable_bytes(downloaded),
                        tools.readable_bytes(total_length)))
        if os.path.exists(job_file):
            entry = self.clientdb.get('local_job_repo_' + job_id)
            entry['status'] = 'downloaded'
            self.clientdb.put('local_job_repo_' + job_id, entry)
            return True
        else:
            return False

    def execute_job(self, job_id):
        print('Executing {}'.format(job_id))
        self.set_status("Executing...")
        import docker
        client = docker.from_env()
        job_directory = os.path.join(self.engine.working_dir, 'jobs', job_id)
        job_directory = os.path.abspath(job_directory)
        result_file = os.path.join(job_directory, 'result.zip')

        config_file = os.path.join(job_directory, 'config.json')
        json.dump(self.engine.config['coinami'], open(config_file, "w"))

        container = client.containers.run(self.engine.config['coinami']['container'], user=os.getuid(), volumes={
            job_directory: {'bind': '/input', 'mode': 'rw'}
        }, detach=True)
        while client.containers.get(container.id).status == 'running' or \
                        client.containers.get(container.id).status == 'created':
            self.set_status('Executing...', client.containers.get(container.id).logs().decode())
            if not self.threaded_running():
                client.containers.get(container.id).kill()
            time.sleep(1)
        if os.path.exists(result_file):
            self.set_status("Executed...")
            entry = self.clientdb.get('local_job_repo_' + job_id)
            entry['status'] = 'executed'
            self.clientdb.put('local_job_repo_' + job_id, entry)
            return True
        else:
            return False

    def upload_job(self, job_id):
        print('Uploading {}'. format(job_id))
        self.set_status("Uploading...")
        job = self.statedb.get_job(job_id)
        auth = self.statedb.get_auth(job['auth'])
        endpoint = auth['host'] + "/job_upload/{}".format(job_id)
        result_directory = os.path.join(self.engine.working_dir, 'jobs', job_id, 'output')
        if not os.path.exists(result_directory):
            os.makedirs(result_directory)
        result_file = os.path.join(result_directory, 'result.zip')
        secret_message = str(uuid.uuid4())
        payload = yaml.dump({
            "message": tools.det_hash(secret_message),
            "signature": tools.sign(tools.det_hash(secret_message), self.wallet.privkey),
            "pubkey": self.wallet.get_pubkey_str()
        })
        files = {'file': open(result_file, 'rb')}
        values = {'payload': payload}

        r = requests.post(endpoint, files=files, data=values)
        if r.status_code == 200 and r.json()['success']:
            entry = self.clientdb.get('local_job_repo_' + job_id)
            self.set_status("Uploaded and Rewarded!")
            entry['status'] = 'uploaded'
            self.clientdb.put('local_job_repo_' + job_id, entry)

    def done_job(self, job_id):
        job = self.statedb.get_job(job_id)
        job_directory = os.path.join(self.engine.working_dir, 'jobs', job_id)
        if os.path.exists(job_directory):
            self.set_status("Cleaning job...")
            import shutil
            shutil.rmtree(job_directory)

        entry = self.clientdb.get('local_job_repo_' + job_id)
        entry['status'] = 'done'
        self.clientdb.put('local_job_repo_' + job_id, entry)

    @threaded
    def worker(self):
        """
        - Find our assigned task.
        - Query Job repository for the task.
        - If job is not downloaded:
            - Download the job.
        - Start container.
        - Upload the result.
        - Mark the job as done.
            - Remove the downloaded files
        - Repeat
        """
        if self.clientdb.get_default_wallet() is None:
            time.sleep(1)
            return

        from halocoin.model.wallet import Wallet
        default_wallet_info = self.clientdb.get_default_wallet()
        encrypted_wallet_content = self.clientdb.get_wallet(default_wallet_info['wallet_name'])
        self.wallet = Wallet.from_string(tools.decrypt(default_wallet_info['password'], encrypted_wallet_content))
        own_address = self.wallet.address
        own_account = self.statedb.get_account(own_address)
        assigned_job = own_account['assigned_job']
        if assigned_job == '':
            self.set_status("No assignment!")
            time.sleep(1)
            return

        job_status = self.get_job_status(assigned_job)
        if job_status == 'null':
            self.set_status("Job assigned! Initializing...")
            self.initiate_job(assigned_job)
        elif job_status == 'assigned':
            self.set_status("Downloading...")
            self.download_job(assigned_job)
        elif job_status == 'downloaded':
            self.execute_job(assigned_job)
        elif job_status == 'executed':
            self.upload_job(assigned_job)
        elif job_status == 'uploaded':
            self.done_job(assigned_job)
        elif job_status == 'done':
            self.set_status("Done!")

        time.sleep(0.1)

    @staticmethod
    def system_status(container_name):
        try:
            client = docker.from_env()
            if not client.ping():
                return Response(False, "Docker daemon is not responding")
            client.images.get(container_name)
        except ImageNotFound:
            return Response(False, "Image missing")
        except:
            return Response(False, "An expection occurred")

        return Response(True, "Ready to go!")
